import argparse
import random
import math
from collections import deque, namedtuple
from datetime import datetime
from warnings import simplefilter
from pathlib import Path

import gymnasium as gym
import numpy as np
from tqdm import tqdm

import torch
from torch import optim, nn
from torch.nn.functional import mse_loss
from torch.utils.tensorboard.writer import SummaryWriter

simplefilter(action="ignore", category=DeprecationWarning)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", type=str, default="LunarLander-v2")
    parser.add_argument("--total-timesteps", type=int, default=500000)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--buffer-size", type=int, default=10000)
    parser.add_argument("--learning-rate", type=float, default=2.5e-4)
    parser.add_argument('--list-layer', nargs="+", type=int, default=[64, 64])
    parser.add_argument("--gamma", type=float, default=0.99)
    parser.add_argument("--eps-end", type=float, default=0.05)
    parser.add_argument("--eps-start", type=int, default=1)
    parser.add_argument("--eps-decay", type=int, default=50000)
    parser.add_argument("--target_network_frequency", type=int, default=500)
    parser.add_argument("--learning-start", type=int, default=10000)
    parser.add_argument("--train-frequency", type=int, default=10)
    parser.add_argument("--cpu", action="store_true")
    parser.add_argument("--capture-video", action="store_true")
    parser.add_argument("--seed", type=int, default=0)

    _args = parser.parse_args()

    _args.device = torch.device(
        "cpu" if _args.cpu or not torch.cuda.is_available() else "cuda")

    return _args


def make_env(env_id, run_dir, capture_video):

    def thunk():

        if capture_video:
            env = gym.make(env_id, render_mode="rgb_array")
            env = gym.wrappers.RecordVideo(env=env,
                                           video_folder=f"{run_dir}/videos/",
                                           disable_logger=True)
        else:
            env = gym.make(env_id)
        env = gym.wrappers.RecordEpisodeStatistics(env)
        env = gym.wrappers.FlattenObservation(env)
        env = gym.wrappers.NormalizeObservation(env)
        env = gym.wrappers.TransformObservation(
            env, lambda obs: np.clip(obs, -10, 10))
        env = gym.wrappers.NormalizeReward(env, gamma=0.99)
        env = gym.wrappers.TransformReward(
            env, lambda reward: np.clip(reward, -10, 10))

        return env

    return thunk


class ReplayMemory():

    def __init__(self, buffer_size, batch_size, obversation_shape, device):
        self.buffer = deque(maxlen=buffer_size)
        self.batch_size = batch_size
        self.obversation_shape = obversation_shape
        self.device = device

        self.transition = namedtuple(
            "Transition",
            field_names=["state", "action", "reward", "next_state", "flag"])

    def push(self, state, action, reward, next_state, flag):
        self.buffer.append(
            self.transition(state, action, reward, next_state, flag))

    def sample(self):
        batch = self.transition(*zip(
            *random.sample(self.buffer, self.batch_size)))

        states = torch.cat(batch.state).to(self.device)
        actions = torch.stack(batch.action).to(self.device)
        rewards = torch.cat(batch.reward).to(self.device)
        next_states = torch.cat(batch.next_state).to(self.device)
        flags = torch.cat(batch.flag).to(self.device)

        return states, actions, rewards, next_states, flags


class QNetwork(nn.Module):

    def __init__(self, args, obversation_space, action_space):

        super().__init__()

        current_layer_value = np.array(obversation_space.shape).prod()
        num_actions = action_space.n

        self.network = nn.Sequential()

        for layer_value in args.list_layer:
            self.network.append(nn.Linear(current_layer_value, layer_value))
            self.network.append(nn.ReLU())
            current_layer_value = layer_value

        self.network.append(nn.Linear(current_layer_value, num_actions))

        self.optimizer = optim.Adam(self.parameters(), lr=args.learning_rate)

        if args.device.type == "cuda":
            self.cuda()

    def forward(self, state):
        return self.network(state)


def main():
    args = parse_args()

    date = str(datetime.now().strftime("%d-%m_%H:%M:%S"))
    run_dir = Path(
        Path(__file__).parent.resolve().parent, "runs",
        f"{args.env}__ppo__{date}")
    writer = SummaryWriter(run_dir)
    writer.add_text(
        "hyperparameters",
        "|param|value|\n|-|-|\n%s" %
        ("\n".join([f"|{key}|{value}|" for key, value in vars(args).items()])),
    )

    if args.seed > 0:
        random.seed(args.seed)
        np.random.seed(args.seed)
        torch.manual_seed(args.seed)

    # Create vectorized environment(s)
    env = gym.vector.SyncVectorEnv(
        [make_env(args.env, run_dir, args.capture_video)])

    obversation_space = env.single_observation_space
    action_space = env.single_action_space

    policy_net = QNetwork(args, obversation_space, action_space)
    target_net = QNetwork(args, obversation_space, action_space)
    target_net.load_state_dict(policy_net.state_dict())

    replay_memory = ReplayMemory(args.buffer_size, args.batch_size,
                                 obversation_space.shape, args.device)

    if args.seed > 0:
        state, _ = env.reset(seed=args.seed)
    else:
        state, _ = env.reset()

    state = torch.from_numpy(state).to(args.device).float()

    for global_step in tqdm(range(args.total_timesteps)):

        # Generate transitions
        with torch.no_grad():
            eps_threshold = args.eps_end + (
                args.eps_start - args.eps_end) * math.exp(
                    -1. * global_step / args.eps_decay)

            writer.add_scalar("rollout/eps_threshold", eps_threshold,
                              global_step)

            # Choice between exploration or intensification
            if np.random.rand() < eps_threshold:
                action = torch.tensor([env.single_action_space.sample()
                                       ]).to(args.device)
            else:
                q_values = policy_net(state)
                action = torch.argmax(q_values, dim=1)

        next_state, reward, terminated, truncated, infos = env.step(
            action.cpu().numpy())

        done = torch.from_numpy(np.logical_or(terminated, truncated)).to(
            args.device).float()
        reward = torch.from_numpy(reward).to(args.device).float()
        next_state = torch.from_numpy(next_state).to(args.device).float()

        replay_memory.push(state, action, reward, next_state, done)

        state = next_state

        if "final_info" in infos:
            info = infos["final_info"][0]
            writer.add_scalar("rollout/episodic_return", info["episode"]["r"],
                              global_step)
            writer.add_scalar("rollout/episodic_length", info["episode"]["l"],
                              global_step)

        # Update policy
        if global_step > args.learning_start:
            if global_step % args.train_frequency == 0:
                states, actions, rewards, next_states, flags = replay_memory.sample(
                )

                # Q values predicted by the model
                td_predict = policy_net(states).gather(1, actions).squeeze()

                with torch.no_grad():
                    # Expected Q values are estimated from actions
                    # which gives maximum Q value
                    action_by_qvalue = policy_net(next_states).argmax(
                        1).unsqueeze(-1)
                    max_q_target = target_net(next_states).gather(
                        1, action_by_qvalue).squeeze()

                # Apply Bellman equation
                td_target = rewards + (1. - flags) * args.gamma * max_q_target

                # Loss is measured from error between current and newly
                # expected Q values
                loss = mse_loss(td_predict, td_target)

                # Backpropagation of loss to NN
                policy_net.optimizer.zero_grad()
                loss.backward()
                policy_net.optimizer.step()

                writer.add_scalar("update/loss", loss, global_step)

                if global_step % args.target_network_frequency == 0:
                    target_net.load_state_dict(policy_net.state_dict())

    env.close()
    writer.close()


if __name__ == '__main__':
    main()