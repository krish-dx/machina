"""
An example of Soft Actor Critic.
"""

import argparse
import copy
import json
import os
from pprint import pprint

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import gym

import machina as mc
from machina.pols import GaussianPol
from machina.algos import sac
from machina.vfuncs import DeterministicSAVfunc
from machina.envs import GymEnv
from machina.traj import Traj
from machina.traj import epi_functional as ef
from machina.samplers import EpiSampler
from machina import logger
from machina.utils import set_device, measure

from simple_net import PolNet, QNet, VNet


parser = argparse.ArgumentParser()
parser.add_argument('--log', type=str, default='garbage')
parser.add_argument('--env_name', type=str, default='Pendulum-v0')
parser.add_argument('--record', action='store_true', default=False)
parser.add_argument('--seed', type=int, default=256)
parser.add_argument('--max_episodes', type=int, default=1000000)
parser.add_argument('--num_parallel', type=int, default=4)
parser.add_argument('--cuda', type=int, default=-1)
parser.add_argument('--data_parallel', action='store_true', default=False)

parser.add_argument('--max_steps_per_iter', type=int, default=10000)
parser.add_argument('--batch_size', type=int, default=256)
parser.add_argument('--sampling', type=int, default=1)
parser.add_argument('--no_reparam', action='store_true', default=False)
parser.add_argument('--pol_lr', type=float, default=1e-4)
parser.add_argument('--qf_lr', type=float, default=3e-4)

parser.add_argument('--ent_alpha', type=float, default=1)
parser.add_argument('--tau', type=float, default=5e-3)
parser.add_argument('--gamma', type=float, default=0.99)
args = parser.parse_args()

if not os.path.exists(args.log):
    os.mkdir(args.log)

with open(os.path.join(args.log, 'args.json'), 'w') as f:
    json.dump(vars(args), f)
pprint(vars(args))

if not os.path.exists(os.path.join(args.log, 'models')):
    os.mkdir(os.path.join(args.log, 'models'))

np.random.seed(args.seed)
torch.manual_seed(args.seed)

device_name = 'cpu' if args.cuda < 0 else "cuda:{}".format(args.cuda)
device = torch.device(device_name)
set_device(device)

score_file = os.path.join(args.log, 'progress.csv')
logger.add_tabular_output(score_file)

env = GymEnv(args.env_name, log_dir=os.path.join(
    args.log, 'movie'), record_video=args.record)
env.env.seed(args.seed)

ob_space = env.observation_space
ac_space = env.action_space

pol_net = PolNet(ob_space, ac_space)
pol = GaussianPol(ob_space, ac_space, pol_net,
                  data_parallel=args.data_parallel, parallel_dim=0)

qf_net1 = QNet(ob_space, ac_space)
qf1 = DeterministicSAVfunc(ob_space, ac_space, qf_net1,
                           data_parallel=args.data_parallel, parallel_dim=0)
targ_qf_net1 = QNet(ob_space, ac_space)
targ_qf_net1.load_state_dict(qf_net1.state_dict())
targ_qf1 = DeterministicSAVfunc(
    ob_space, ac_space, targ_qf_net1, data_parallel=args.data_parallel, parallel_dim=0)

qf_net2 = QNet(ob_space, ac_space)
qf2 = DeterministicSAVfunc(ob_space, ac_space, qf_net2,
                           data_parallel=args.data_parallel, parallel_dim=0)
targ_qf_net2 = QNet(ob_space, ac_space)
targ_qf_net2.load_state_dict(qf_net2.state_dict())
targ_qf2 = DeterministicSAVfunc(
    ob_space, ac_space, targ_qf_net2, data_parallel=args.data_parallel, parallel_dim=0)

qfs = [qf1, qf2]
targ_qfs = [targ_qf1, targ_qf2]

log_alpha = nn.Parameter(torch.zeros((), device=device))

sampler = EpiSampler(env, pol, args.num_parallel, seed=args.seed)

optim_pol = torch.optim.Adam(pol_net.parameters(), args.pol_lr)
optim_qf1 = torch.optim.Adam(qf_net1.parameters(), args.qf_lr)
optim_qf2 = torch.optim.Adam(qf_net2.parameters(), args.qf_lr)
optim_qfs = [optim_qf1, optim_qf2]
optim_alpha = torch.optim.Adam([log_alpha], args.pol_lr)

off_traj = Traj()

total_epi = 0
total_step = 0
max_rew = -1e6

while args.max_episodes > total_epi:
    with measure('sample'):
        epis = sampler.sample(pol, max_steps=args.max_steps_per_iter)

    with measure('train'):
        on_traj = Traj()
        on_traj.add_epis(epis)

        on_traj = ef.add_next_obs(on_traj)
        on_traj.register_epis()

        off_traj.add_traj(on_traj)

        total_epi += on_traj.num_epi
        step = on_traj.num_step
        total_step += step

        if args.data_parallel:
            pol.dp_run = True
            qf.dp_run = True

        result_dict = sac.train(
            off_traj,
            pol, qfs, targ_qfs, log_alpha,
            optim_pol, optim_qfs, optim_alpha,
            step, args.batch_size,
            args.tau, args.gamma, args.sampling, not args.no_reparam
        )

        if args.data_parallel:
            pol.dp_run = False
            qf.dp_run = False

    rewards = [np.sum(epi['rews']) for epi in epis]
    mean_rew = np.mean(rewards)
    logger.record_results(args.log, result_dict, score_file,
                          total_epi, step, total_step,
                          rewards,
                          plot_title=args.env_name)

    if mean_rew > max_rew:
        torch.save(pol.state_dict(), os.path.join(
            args.log, 'models', 'pol_max.pkl'))
        torch.save(qf1.state_dict(), os.path.join(
            args.log, 'models', 'qf1_max.pkl'))
        torch.save(qf2.state_dict(), os.path.join(
            args.log, 'models', 'qf2_max.pkl'))
        torch.save(optim_pol.state_dict(), os.path.join(
            args.log, 'models', 'optim_pol_max.pkl'))
        torch.save(optim_qf1.state_dict(), os.path.join(
            args.log, 'models', 'optim_qf1_max.pkl'))
        torch.save(optim_qf2.state_dict(), os.path.join(
            args.log, 'models', 'optim_qf2_max.pkl'))
        max_rew = mean_rew

    torch.save(pol.state_dict(), os.path.join(
        args.log, 'models', 'pol_last.pkl'))
    torch.save(qf1.state_dict(), os.path.join(
        args.log, 'models', 'qf1_last.pkl'))
    torch.save(qf2.state_dict(), os.path.join(
        args.log, 'models', 'qf2_last.pkl'))
    torch.save(optim_pol.state_dict(), os.path.join(
        args.log, 'models', 'optim_pol_last.pkl'))
    torch.save(optim_qf1.state_dict(), os.path.join(
        args.log, 'models', 'optim_qf1_last.pkl'))
    torch.save(optim_qf2.state_dict(), os.path.join(
        args.log, 'models', 'optim_qf2_last.pkl'))
    del on_traj
del sampler
