from copy import deepcopy
import numpy as np
import torch
from torch.optim import Adam
import os
import gym
import time
import spinup.algos.pytorch.ddpg.core as core
from spinup.utils.logx import EpochLogger
from src.utils import *


class ReplayBuffer:
    """
    A simple FIFO experience replay buffer for DDPG agents.
    """

    def __init__(self, obs_dim, act_dim, size):
        self.obs_buf = np.zeros(core.combined_shape(size, obs_dim), dtype=np.float32)
        self.obs2_buf = np.zeros(core.combined_shape(size, obs_dim), dtype=np.float32)
        self.act_buf = np.zeros(core.combined_shape(size, act_dim), dtype=np.float32)
        self.rew_buf = np.zeros(size, dtype=np.float32)
        self.done_buf = np.zeros(size, dtype=np.float32)
        self.ptr, self.size, self.max_size = 0, 0, size

    def store(self, obs, act, rew, next_obs, done):
        self.obs_buf[self.ptr] = obs
        self.obs2_buf[self.ptr] = next_obs
        self.act_buf[self.ptr] = act
        self.rew_buf[self.ptr] = rew
        self.done_buf[self.ptr] = done
        self.ptr = (self.ptr+1) % self.max_size
        self.size = min(self.size+1, self.max_size)

    def sample_batch(self, batch_size=32):
        idxs = np.random.randint(0, self.size, size=batch_size)
        batch = dict(obs=self.obs_buf[idxs],
                     obs2=self.obs2_buf[idxs],
                     act=self.act_buf[idxs],
                     rew=self.rew_buf[idxs],
                     done=self.done_buf[idxs])
        return {k: torch.as_tensor(v, dtype=torch.float32) for k,v in batch.items()}



def ddpg(env_fn, mode='train', actor_critic=None, ac_kwargs=dict(), replay_buffer=None,replay_buffer_kwargs=dict(), seed=0, steps_per_epoch=4000, epochs=100, replay_size=int(1e6), gamma=0.99, polyak=0.995, pi_lr=1e-3, q_lr=1e-3, batch_size=100, start_steps=10000, update_after=1000, update_every=50, act_noise=0.1, num_test_episodes=10, max_ep_len=1000, logger=None, logger_kwargs=None, save_freq=1, device='cpu'):
    """
    Deep Deterministic Policy Gradient (DDPG)


    Args:
        env_fn : A function which creates a copy of the environment.
            The environment must satisfy the OpenAI Gym API.

        actor_critic: The constructor method for a PyTorch Module with an ``act``
            method, a ``pi`` module, and a ``q`` module. The ``act`` method and
            ``pi`` module should accept batches of observations as inputs,
            and ``q`` should accept a batch of observations and a batch of
            actions as inputs. When called, these should return:

            ===========  ================  ======================================
            Call         Output Shape      Description
            ===========  ================  ======================================
            ``act``      (batch, act_dim)  | Numpy array of actions for each
                                           | observation.
            ``pi``       (batch, act_dim)  | Tensor containing actions from policy
                                           | given observations.
            ``q``        (batch,)          | Tensor containing the current estimate
                                           | of Q* for the provided observations
                                           | and actions. (Critical: make sure to
                                           | flatten this!)
            ===========  ================  ======================================

        ac_kwargs (dict): Any kwargs appropriate for the ActorCritic object
            you provided to DDPG.

        seed (int): Seed for random number generators.

        steps_per_epoch (int): Number of steps of interaction (state-action pairs)
            for the agent and the environment in each epoch.

        epochs (int): Number of epochs to run and train agent.

        replay_size (int): Maximum length of replay buffer.

        gamma (float): Discount factor. (Always between 0 and 1.)

        polyak (float): Interpolation factor in polyak averaging for target
            networks. Target networks are updated towards main networks
            according to:

            .. math:: \\theta_{\\text{targ}} \\leftarrow
                \\rho \\theta_{\\text{targ}} + (1-\\rho) \\theta

            where :math:`\\rho` is polyak. (Always between 0 and 1, usually
            close to 1.)

        pi_lr (float): Learning rate for policy.

        q_lr (float): Learning rate for Q-networks.

        batch_size (int): Minibatch size for SGD.

        start_steps (int): Number of steps for uniform-random action selection,
            before running real policy. Helps exploration.

        update_after (int): Number of env interactions to collect before
            starting to do gradient descent updates. Ensures replay buffer
            is full enough for useful updates.

        update_every (int): Number of env interactions that should elapse
            between gradient descent updates. Note: Regardless of how long
            you wait between updates, the ratio of env steps to gradient steps
            is locked to 1.

        act_noise (float): Stddev for Gaussian exploration noise added to
            policy at training time. (At test time, no noise is added.)

        num_test_episodes (int): Number of episodes to test the deterministic
            policy at the end of each epoch.

        max_ep_len (int): Maximum length of trajectory / episode / rollout.

        logger_kwargs (dict): Keyword args for EpochLogger.

        save_freq (int): How often (in terms of gap between epochs) to save
            the current policy and value function.

    """

    torch.manual_seed(seed)
    np.random.seed(seed)

    env, test_env = env_fn('train'), env_fn('test')

    # ==================================
    # Creating logging folders
    # ==================================
    output_dir = logger_kwargs['output_dir']
    exp_name = output_dir.split('/')[1]

    try:
        chkpt_dir = os.path.join(output_dir, 'checkpoints')
        os.makedirs(chkpt_dir)
        log(f'Created {chkpt_dir}', 'green')
    except:
        pass

    try:
        tb_dir = os.path.join(output_dir, 'tensorboard')
        os.makedirs(tb_dir)
        log(f'Created {tb_dir}', 'green')
    except:
        pass

    try:
        vid_dir = os.path.join(output_dir, 'videos')
        os.makedirs(vid_dir)
        log(f'Created {vid_dir}', 'green')
        test_env.set_save_dir(vid_dir)
    except:
        pass

    if not logger:
        logger = EpochLogger(**logger_kwargs)
    else:
        logger = logger(**logger_kwargs)

    # ********************* CUSTOM ********************* #
    obs_dim = sum([env.observation_space.spaces[k].shape[0] for k in env.observation_space.spaces.keys()])
    act_dim = env.action_space['action'].shape[0]

    # Action limit for clamping: critically, assumes all dimensions share the same bound!
    act_limit = env.action_space['action'].high[0]

    # Create actor-critic module and target networks
    if actor_critic is None:
        ac = core.MLPActorCritic(env.observation_space, env.action_space['action'], **ac_kwargs)
    else:
        ac = actor_critic(**ac_kwargs)

    ac_targ = deepcopy(ac)

    # Put on CUDA
    ac = ac.to(device)
    ac_targ = ac_targ.to(device)

    # Set up optimizers for policy and q-function
    pi_optimizer = Adam(ac.pi.parameters(), lr=pi_lr)
    q_optimizer = Adam(ac.q.parameters(), lr=q_lr)

    # Set up model saving
    logger.setup_pytorch_saver(ac)

    # Load from checkpoint if eval
    if mode == 'eval':
        chkpt_file = os.path.join(logger_kwargs['output_dir'], 'checkpoints', 'ckpt_1_-183.19.pth')
        checkpoint = torch.load(chkpt_file)
        ac.load_state_dict(checkpoint['model_state_dict'])
        ac_targ.load_state_dict(checkpoint['model_state_dict'])
        pi_optimizer.load_state_dict(checkpoint['pi_optimizer_state_dict'])
        q_optimizer.load_state_dict(checkpoint['q_optimizer_state_dict'])
        ac.eval()
        ac_targ.eval()

    # Freeze target networks with respect to optimizers (only update via polyak averaging)
    for p in ac_targ.parameters():
        p.requires_grad = False

    # Experience buffer
    if not replay_buffer:
        replay_buffer = ReplayBuffer(obs_dim=obs_dim, act_dim=act_dim, size=replay_size)
    else:
        replay_buffer = replay_buffer(**replay_buffer_kwargs)

    # Count variables (protip: try to get a feel for how different size networks behave!)
    var_counts = tuple(core.count_vars(module) for module in [ac.pi, ac.q])
    logger.log('\nNumber of parameters: \t pi: %d, \t q: %d\n'%var_counts)

    # Set up function for computing DDPG Q-loss
    def compute_loss_q(data):
        o, a, r, o2, d = data['obs'], data['act'], data['rew'], data['obs2'], data['done']

        q = ac.q(o,a)

        # Bellman backup for Q function
        with torch.no_grad():
            q_pi_targ = ac_targ.q(o2, ac_targ.pi(o2))
            backup = r + gamma * (1 - d) * q_pi_targ

        # MSE loss against Bellman backup
        loss_q = ((q - backup)**2).mean()

        # Useful info for logging
        loss_info = dict(q_vals=q.detach().cpu().numpy())

        return loss_q, loss_info

    # Set up function for computing DDPG pi loss
    def compute_loss_pi(data):
        o = data['obs']
        q_pi = ac.q(o, ac.pi(o))
        return -q_pi.mean()

    def update(data):
        # First run one gradient descent step for Q.
        q_optimizer.zero_grad()
        loss_q, loss_info = compute_loss_q(data)
        loss_q.backward()
        q_optimizer.step()

        # Freeze Q-network so you don't waste computational effort
        # computing gradients for it during the policy learning step.
        for p in ac.q.parameters():
            p.requires_grad = False

        # Next run one gradient descent step for pi.
        pi_optimizer.zero_grad()
        loss_pi = compute_loss_pi(data)
        loss_pi.backward()
        pi_optimizer.step()

        # Unfreeze Q-network so you can optimize it at next DDPG step.
        for p in ac.q.parameters():
            p.requires_grad = True

        # Record things
        logger.store(loss_critic=loss_q.item(), loss_policy=loss_pi.item(), **loss_info)

        # Finally, update target networks by polyak averaging.
        with torch.no_grad():
            for p, p_targ in zip(ac.parameters(), ac_targ.parameters()):
                # NB: We use an in-place operations "mul_", "add_" to update target
                # params, as opposed to "mul" and "add", which would make new tensors.
                p_targ.data.mul_(polyak)
                p_targ.data.add_((1 - polyak) * p.data)

    def get_action(o, noise_scale):
        a = ac.act(o)
        a += noise_scale * np.random.randn(act_dim)
        # this used to be -act_limit
        return np.clip(a, 0, act_limit)

    def test_agent():
        for j in range(num_test_episodes):
            o, d, ep_ret, ep_len = test_env.reset(), False, 0, 0
            while not(d or (ep_len == max_ep_len)):
                # Take deterministic actions at test time (noise_scale=0)
                o, r, d, _ = test_env.step(get_action(o, 0))
                ep_ret += r
                ep_len += 1
            logger.store(eval_episode_return=ep_ret, eval_episode_length=ep_len)

    def save_model(epoch, key):
        savepath = os.path.join(logger_kwargs['output_dir'], 'checkpoints', f'ckpt_{epoch}_{key:.2f}.pth')

        print(f'Saving model to {savepath}')

        torch.save({
            'epoch': epoch,
            'model_state_dict': ac.state_dict(),
            'pi_optimizer_state_dict': pi_optimizer.state_dict(),
            'q_optimizer_state_dict': q_optimizer.state_dict()
        }, savepath)

    if mode == 'train':
        # Prepare for interaction with environment
        total_steps = steps_per_epoch * epochs
        start_time = time.time()
        o, ep_ret, ep_len = env.reset(), 0, 0

        prev_best = float('-inf')

        # Main loop: collect experience in env and update/log each epoch
        for t in range(total_steps):

            # Until start_steps have elapsed, randomly sample actions
            # from a uniform distribution for better exploration. Afterwards,
            # use the learned policy (with some noise, via act_noise).
            if t > start_steps:
                a = get_action(o, act_noise)
            else:
                a = env.action_space['action'].sample()

            # Step the env
            o2, r, d, _ = env.step(a)

            # if t > start_steps:
            #     print('='*40)
            #     print(o2, r, a)

            ep_ret += r
            ep_len += 1

            # Ignore the "done" signal if it comes from hitting the time
            # horizon (that is, when it's an artificial terminal signal
            # that isn't based on the agent's state)
            d = False if ep_len==max_ep_len else d

            # Store experience to replay buffer
            replay_buffer.store(o, a, r, o2, d)

            # Super critical, easy to overlook step: make sure to update
            # most recent observation!
            o = o2

            # End of trajectory handling
            if d or (ep_len == max_ep_len):
                logger.store(episode_return=ep_ret, episode_length=ep_len)
                o, ep_ret, ep_len = env.reset(), 0, 0

            # Update handling
            if t >= update_after and t % update_every == 0:
                for _ in range(update_every):
                    batch = replay_buffer.sample_batch(batch_size)
                    update(data=batch)

            # End of epoch handling
            if (t+1) % steps_per_epoch == 0:
                epoch = (t+1) // steps_per_epoch

                # Save model
                if (epoch % save_freq == 0) or (epoch == epochs):
                    logger.save_state({'env': env}, None)

                # Test the performance of the deterministic version of the agent.
                test_agent()

                avg_episode_ret = np.mean(logger.epoch_dict['episode_return'])

                # Log info about epoch
                logger.log_tabular('epoch', epoch)
                logger.log_epoch_stats(epoch, 'train', 'episode_return', with_min_and_max=True)
                logger.log_epoch_stats(epoch, 'eval', 'eval_episode_return', with_min_and_max=True)
                logger.log_epoch_stats(epoch, 'train', 'episode_length', average_only=True)
                logger.log_epoch_stats(epoch, 'eval', 'eval_episode_length', average_only=True)
                logger.log_epoch_stats(epoch, 'train', 'total_interactions', t)
                logger.log_epoch_stats(epoch, 'rl', 'q_vals', with_min_and_max=True)
                logger.log_epoch_stats(epoch, 'rl', 'loss_policy', average_only=True)
                logger.log_epoch_stats(epoch, 'rl', 'loss_critic', average_only=True)
                logger.log_tabular('time_elapsed', time.time()-start_time)
                logger.dump_tabular()

                if avg_episode_ret > prev_best:
                    print(f'Prev best {prev_best}, new best {avg_episode_ret}')
                    save_model(epoch=epoch, key=avg_episode_ret)
                prev_best = max(prev_best, avg_episode_ret)
    elif mode == 'eval':
        test_agent()

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--env', type=str, default='HalfCheetah-v2')
    parser.add_argument('--hid', type=int, default=256)
    parser.add_argument('--l', type=int, default=2)
    parser.add_argument('--gamma', type=float, default=0.99)
    parser.add_argument('--seed', '-s', type=int, default=0)
    parser.add_argument('--epochs', type=int, default=50)
    parser.add_argument('--exp_name', type=str, default='ddpg')
    args = parser.parse_args()

    from spinup.utils.run_utils import setup_logger_kwargs
    logger_kwargs = setup_logger_kwargs(args.exp_name, args.seed)

    ddpg(lambda : gym.make(args.env), actor_critic=core.MLPActorCritic,
         ac_kwargs=dict(hidden_sizes=[args.hid]*args.l),
         gamma=args.gamma, seed=args.seed, epochs=args.epochs,
         logger_kwargs=logger_kwargs)