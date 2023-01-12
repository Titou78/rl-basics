import argparse
import random
from collections import deque, namedtuple
from datetime import datetime
from pathlib import Path
from warnings import simplefilter

import gymnasium as gym
import numpy as np
import torch
from torch import nn, optim
from torch.distributions import Uniform
from torch.nn.functional import mse_loss
from torch.utils.tensorboard.writer import SummaryWriter
from tqdm import tqdm

simplefilter(action="ignore", category=DeprecationWarning)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", type=str, default="HalfCheetah-v4")
    parser.add_argument("--total-timesteps", type=int, default=int(1e6))
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--buffer-size", type=int, default=int(1e5))
    parser.add_argument("--learning-rate", type=float, default=3e-4)
    parser.add_argument("--list-layer", nargs="+", type=int, default=[64, 64])
    parser.add_argument("--gamma", type=float, default=0.99)
    parser.add_argument("--tau", type=float, default=0.005)
    parser.add_argument("--exploration-noise", type=float, default=0.1)
    parser.add_argument("--noise-clip", type=float, default=0.5)
    parser.add_argument("--policy-noise", type=float, default=0.2)
    parser.add_argument("--learning-start", type=int, default=25000)
    parser.add_argument("--policy-frequency", type=int, default=4)
    parser.add_argument("--cpu", action="store_true")
    parser.add_argument("--capture-video", action="store_true")
    parser.add_argument("--seed", type=int, default=0)

    _args = parser.parse_args()

    _args.device = torch.device("cpu" if _args.cpu or not torch.cuda.is_available() else "cuda")

    return _args


def make_env(env_id, run_dir, capture_video):
    def thunk():

        if capture_video:
            env = gym.make(env_id, render_mode="rgb_array")
            env = gym.wrappers.RecordVideo(
                env=env, video_folder=f"{run_dir}/videos/", disable_logger=True
            )
        else:
            env = gym.make(env_id)
        env = gym.wrappers.RecordEpisodeStatistics(env)
        env = gym.wrappers.ClipAction(env)
        env = gym.wrappers.FlattenObservation(env)
        env = gym.wrappers.NormalizeObservation(env)
        env = gym.wrappers.TransformObservation(env, lambda obs: np.clip(obs, -10, 10))
        env = gym.wrappers.NormalizeReward(env, gamma=0.99)
        env = gym.wrappers.TransformReward(env, lambda reward: np.clip(reward, -10, 10))

        return env

    return thunk


class ReplayBuffer:
    def __init__(self, buffer_size, batch_size, obversation_shape, device):
        self.buffer = deque(maxlen=buffer_size)
        self.batch_size = batch_size
        self.obversation_shape = obversation_shape
        self.device = device

        self.transition = namedtuple(
            "Transition", field_names=["state", "action", "reward", "next_state", "flag"]
        )

    def push(self, state, action, reward, next_state, flag):
        self.buffer.append(self.transition(state, action, reward, next_state, flag))

    def sample(self):
        batch = self.transition(*zip(*random.sample(self.buffer, self.batch_size)))

        states = torch.cat(batch.state).to(self.device)
        actions = torch.cat(batch.action).to(self.device)
        rewards = torch.cat(batch.reward).to(self.device)
        next_states = torch.cat(batch.next_state).to(self.device)
        flags = torch.cat(batch.flag).to(self.device)

        return states, actions, rewards, next_states, flags


class ActorNet(nn.Module):
    def __init__(self, args, obversation_shape, action_shape, action_low, action_high):
        super().__init__()

        obversation_shape = np.prod(obversation_shape)
        action_shape = np.prod(action_shape)

        self.network = nn.Sequential(
            nn.Linear(obversation_shape, 256),
            nn.ReLU(),
            nn.Linear(256, 256),
            nn.ReLU(),
            nn.Linear(256, action_shape),
        )

        # action rescaling
        self.register_buffer("action_scale", ((action_high - action_low) / 2.0))
        self.register_buffer("action_bias", ((action_high + action_low) / 2.0))

        self.optimizer = optim.Adam(self.parameters(), lr=args.learning_rate)

        if args.device.type == "cuda":
            self.cuda()

    def forward(self, state):
        output = torch.tanh(self.network(state))
        return output * self.action_scale + self.action_bias


class CriticNet(nn.Module):
    def __init__(self, args, obversation_shape, action_shape):
        super().__init__()

        input_shape = np.prod(obversation_shape) + np.prod(action_shape)

        self.network = nn.Sequential(
            nn.Linear(input_shape, 256),
            nn.ReLU(),
            nn.Linear(256, 256),
            nn.ReLU(),
            nn.Linear(256, 1),
        )

        if args.device.type == "cuda":
            self.cuda()

    def forward(self, state, action):
        input_ = torch.cat([state, action], 1)
        return self.network(input_)


def main():
    args = parse_args()

    date = str(datetime.now().strftime("%d-%m_%H:%M:%S"))
    run_dir = Path(Path(__file__).parent.resolve().parent, "../runs", f"{args.env}__td3__{date}")
    writer = SummaryWriter(run_dir)
    writer.add_text(
        "hyperparameters",
        "|param|value|\n|-|-|\n%s"
        % ("\n".join([f"|{key}|{value}|" for key, value in vars(args).items()])),
    )

    # Seeding
    if args.seed > 0:
        random.seed(args.seed)
        np.random.seed(args.seed)
        torch.manual_seed(args.seed)

    # Create vectorized environment(s)
    env = gym.vector.SyncVectorEnv([make_env(args.env, run_dir, args.capture_video)])

    obversation_shape = env.single_observation_space.shape
    action_shape = env.single_action_space.shape
    action_low = torch.from_numpy(env.single_action_space.low).to(args.device)
    action_high = torch.from_numpy(env.single_action_space.high).to(args.device)

    actor = ActorNet(args, obversation_shape, action_shape, action_low, action_high)
    target_actor = ActorNet(args, obversation_shape, action_shape, action_low, action_high)
    critic1 = CriticNet(args, obversation_shape, action_shape)
    critic1_target = CriticNet(args, obversation_shape, action_shape)
    critic2 = CriticNet(args, obversation_shape, action_shape)
    critic2_target = CriticNet(args, obversation_shape, action_shape)

    target_actor.load_state_dict(actor.state_dict())
    critic1_target.load_state_dict(critic1.state_dict())
    critic2_target.load_state_dict(critic2.state_dict())

    actor_optimizer = optim.Adam(actor.parameters(), lr=args.learning_rate)
    critic_optimizer = optim.Adam(
        list(critic1.parameters()) + list(critic2.parameters()), lr=args.learning_rate
    )

    replay_buffer = ReplayBuffer(args.buffer_size, args.batch_size, obversation_shape, args.device)

    state, _ = env.reset(seed=args.seed) if args.seed > 0 else env.reset()

    state = torch.from_numpy(state).to(args.device).float()

    for global_step in tqdm(range(args.total_timesteps)):

        if global_step < args.learning_start:
            action = Uniform(action_low, action_high).sample().unsqueeze(0)
        else:
            with torch.no_grad():
                action = actor(state)
                action += torch.normal(0, actor.action_scale * args.exploration_noise)

        next_state, reward, terminated, truncated, infos = env.step(action.cpu().numpy())

        next_state = torch.from_numpy(next_state).to(args.device).float()
        reward = torch.from_numpy(reward).to(args.device).float()
        flag = torch.from_numpy(np.logical_or(terminated, truncated)).to(args.device).float()

        replay_buffer.push(state, action, reward, next_state, flag)

        state = next_state

        if "final_info" in infos:
            info = infos["final_info"][0]
            writer.add_scalar("rollout/episodic_return", info["episode"]["r"], global_step)
            writer.add_scalar("rollout/episodic_length", info["episode"]["l"], global_step)

        # Update policy
        if global_step > args.learning_start:
            states, actions, rewards, next_states, flags = replay_buffer.sample()

            # Update critic
            with torch.no_grad():
                clipped_noise = (
                    torch.clamp(
                        (torch.randn_like(actions) * args.policy_noise),
                        -args.noise_clip,
                        args.noise_clip,
                    )
                    * target_actor.action_scale
                )

                next_state_actions = torch.clamp(
                    (target_actor(next_states) + clipped_noise), action_low, action_high
                )
                critic1_next_target = critic1_target(next_states, next_state_actions).squeeze()
                critic2_next_target = critic2_target(next_states, next_state_actions).squeeze()
                min_qf_next_target = torch.min(critic1_next_target, critic2_next_target)
                next_q_value = rewards + (1.0 - flags) * args.gamma * min_qf_next_target

            qf1_a_values = critic1(states, actions).squeeze()
            qf2_a_values = critic2(states, actions).squeeze()
            qf1_loss = mse_loss(qf1_a_values, next_q_value)
            qf2_loss = mse_loss(qf2_a_values, next_q_value)
            critic_loss = qf1_loss + qf2_loss

            critic_optimizer.zero_grad()
            critic_loss.backward()
            critic_optimizer.step()

            # Update actor
            if global_step % args.policy_frequency == 0:
                actor_loss = -critic1(states, actor(states)).mean()
                actor_optimizer.zero_grad()
                actor_loss.backward()
                actor_optimizer.step()

                # Update the target network
                for param, target_param in zip(actor.parameters(), target_actor.parameters()):
                    target_param.data.copy_(
                        args.tau * param.data + (1 - args.tau) * target_param.data
                    )
                for param, target_param in zip(critic1.parameters(), critic1_target.parameters()):
                    target_param.data.copy_(
                        args.tau * param.data + (1 - args.tau) * target_param.data
                    )
                for param, target_param in zip(critic2.parameters(), critic2_target.parameters()):
                    target_param.data.copy_(
                        args.tau * param.data + (1 - args.tau) * target_param.data
                    )

    env.close()
    writer.close()


if __name__ == "__main__":
    main()
