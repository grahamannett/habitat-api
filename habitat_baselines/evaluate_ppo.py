#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import argparse
import glob
import os
import time
from typing import Optional

import torch

import habitat
from config.default import get_config as cfg_baseline
from habitat import logger
from habitat.config.default import get_config
from habitat.utils.visualizations.utils import (
    images_to_video,
    observations_to_image,
)
from rl.ppo import PPO, Policy
from rl.ppo.utils import batch_obs
from tensorboard_utils import get_tensorboard_writer
from train_ppo import make_env_fn


def poll_checkpoint_folder(
    checkpoint_folder: str, previous_ckpt_ind: int
) -> Optional[str]:
    r""" Return (previous_ckpt_ind + 1)th checkpoint in checkpoint folder
    (sorted by time of last modification).

    Args:
        checkpoint_folder: directory to look for checkpoints.
        previous_ckpt_ind: index of checkpoint last returned.

    Returns:
        return checkpoint path if (previous_ckpt_ind + 1)th checkpoint is found
        else return None.
    """
    assert os.path.isdir(checkpoint_folder), "invalid checkpoint folder path"
    models_paths = list(
        filter(os.path.isfile, glob.glob(checkpoint_folder + "/*"))
    )
    models_paths.sort(key=os.path.getmtime)
    ind = previous_ckpt_ind + 1
    if ind < len(models_paths):
        return models_paths[ind]
    return None


def generate_video(
    args, images, episode_id, checkpoint_idx, spl, tb_writer, fps=10
) -> None:
    r"""Generate video according to specified information.

    Args:
        args: contains args.video_option and args.video_dir.
        images: list of images to be converted to video.
        episode_id: episode id for video naming.
        checkpoint_idx: checkpoint index for video naming.
        spl: SPL for this episode for video naming.
        tb_writer: tensorboard writer object for uploading video
        fps: fps for generated video

    Returns:
        None
    """
    if args.video_option and len(images) > 0:
        video_name = f"episode{episode_id}_ckpt{checkpoint_idx}_spl{spl:.2f}"
        if "disk" in args.video_option:
            images_to_video(images, args.video_dir, video_name)
        if "tensorboard" in args.video_option:
            tb_writer.add_video_from_np_images(
                f"episode{episode_id}", checkpoint_idx, images, fps=fps
            )


def eval_checkpoint(checkpoint_path, args, writer, cur_ckpt_idx=0):
    env_configs = []
    baseline_configs = []
    device = torch.device("cuda", args.pth_gpu_id)

    for _ in range(args.num_processes):
        config_env = get_config(config_paths=args.task_config)
        config_env.defrost()
        config_env.DATASET.SPLIT = "val"

        agent_sensors = args.sensors.strip().split(",")
        for sensor in agent_sensors:
            assert sensor in ["RGB_SENSOR", "DEPTH_SENSOR"]
        config_env.SIMULATOR.AGENT_0.SENSORS = agent_sensors
        if args.video_option:
            config_env.TASK.MEASUREMENTS.append("TOP_DOWN_MAP")
            config_env.TASK.MEASUREMENTS.append("COLLISIONS")
        config_env.freeze()
        env_configs.append(config_env)

        config_baseline = cfg_baseline()
        baseline_configs.append(config_baseline)

    assert len(baseline_configs) > 0, "empty list of datasets"

    envs = habitat.VectorEnv(
        make_env_fn=make_env_fn,
        env_fn_args=tuple(
            tuple(
                zip(env_configs, baseline_configs, range(args.num_processes))
            )
        ),
    )

    ckpt = torch.load(checkpoint_path, map_location=device)

    actor_critic = Policy(
        observation_space=envs.observation_spaces[0],
        action_space=envs.action_spaces[0],
        hidden_size=512,
        goal_sensor_uuid=env_configs[0].TASK.GOAL_SENSOR_UUID,
    )
    actor_critic.to(device)

    ppo = PPO(
        actor_critic=actor_critic,
        clip_param=0.1,
        ppo_epoch=4,
        num_mini_batch=32,
        value_loss_coef=0.5,
        entropy_coef=0.01,
        lr=2.5e-4,
        eps=1e-5,
        max_grad_norm=0.5,
    )

    ppo.load_state_dict(ckpt["state_dict"])

    actor_critic = ppo.actor_critic

    observations = envs.reset()
    batch = batch_obs(observations)
    for sensor in batch:
        batch[sensor] = batch[sensor].to(device)

    episode_rewards = torch.zeros(envs.num_envs, 1, device=device)
    episode_spls = torch.zeros(envs.num_envs, 1, device=device)
    episode_success = torch.zeros(envs.num_envs, 1, device=device)
    episode_counts = torch.zeros(envs.num_envs, 1, device=device)
    current_episode_reward = torch.zeros(envs.num_envs, 1, device=device)

    test_recurrent_hidden_states = torch.zeros(
        args.num_processes, args.hidden_size, device=device
    )
    not_done_masks = torch.zeros(args.num_processes, 1, device=device)
    stats_episodes = set()

    rgb_frames = None
    if args.video_option:
        rgb_frames = [[]] * args.num_processes
        os.makedirs(args.video_dir, exist_ok=True)

    while episode_counts.sum() < args.count_test_episodes:
        current_episodes = envs.current_episodes()

        with torch.no_grad():
            _, actions, _, test_recurrent_hidden_states = actor_critic.act(
                batch,
                test_recurrent_hidden_states,
                not_done_masks,
                deterministic=False,
            )

        outputs = envs.step([a[0].item() for a in actions])

        observations, rewards, dones, infos = [list(x) for x in zip(*outputs)]
        batch = batch_obs(observations)
        for sensor in batch:
            batch[sensor] = batch[sensor].to(device)

        not_done_masks = torch.tensor(
            [[0.0] if done else [1.0] for done in dones],
            dtype=torch.float,
            device=device,
        )

        for i in range(not_done_masks.shape[0]):
            if not_done_masks[i].item() == 0:
                episode_spls[i] += infos[i]["spl"]
                if infos[i]["spl"] > 0:
                    episode_success[i] += 1

        rewards = torch.tensor(
            rewards, dtype=torch.float, device=device
        ).unsqueeze(1)
        current_episode_reward += rewards
        episode_rewards += (1 - not_done_masks) * current_episode_reward
        episode_counts += 1 - not_done_masks
        current_episode_reward *= not_done_masks

        next_episodes = envs.current_episodes()
        envs_to_pause = []
        n_envs = envs.num_envs
        for i in range(n_envs):
            if next_episodes[i].episode_id in stats_episodes:
                envs_to_pause.append(i)

            # episode ended
            if not_done_masks[i].item() == 0:
                stats_episodes.add(current_episodes[i].episode_id)
                if args.video_option:
                    generate_video(
                        args,
                        rgb_frames[i],
                        current_episodes[i].episode_id,
                        cur_ckpt_idx,
                        infos[i]["spl"],
                        writer,
                    )
                    rgb_frames[i] = []

            # episode continues
            elif args.video_option:
                frame = observations_to_image(observations[i], infos[i])
                rgb_frames[i].append(frame)

        # stop tracking ended episodes if they exist
        if len(envs_to_pause) > 0:
            state_index = list(range(envs.num_envs))
            for idx in reversed(envs_to_pause):
                state_index.pop(idx)
                envs.pause_at(idx)

            # indexing along the batch dimensions
            test_recurrent_hidden_states = test_recurrent_hidden_states[
                :, state_index
            ]
            not_done_masks = not_done_masks[state_index]
            current_episode_reward = current_episode_reward[state_index]

            for k, v in batch.items():
                batch[k] = v[state_index]

            if args.video_option:
                rgb_frames = [rgb_frames[i] for i in state_index]

    episode_reward_mean = (episode_rewards / episode_counts).mean().item()
    episode_spl_mean = (episode_spls / episode_counts).mean().item()
    episode_success_mean = (episode_success / episode_counts).mean().item()

    logger.info("Average episode reward: {:.6f}".format(episode_reward_mean))
    logger.info("Average episode success: {:.6f}".format(episode_success_mean))
    logger.info("Average episode SPL: {:.6f}".format(episode_spl_mean))

    writer.add_scalars(
        "eval_reward", {"average reward": episode_reward_mean}, cur_ckpt_idx
    )
    writer.add_scalars(
        "eval_SPL", {"average SPL": episode_spl_mean}, cur_ckpt_idx
    )
    writer.add_scalars(
        "eval_success", {"average success": episode_success_mean}, cur_ckpt_idx
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-path", type=str)
    parser.add_argument("--tracking-model-dir", type=str)
    parser.add_argument("--sim-gpu-id", type=int, required=True)
    parser.add_argument("--pth-gpu-id", type=int, required=True)
    parser.add_argument("--num-processes", type=int, required=True)
    parser.add_argument("--hidden-size", type=int, default=512)
    parser.add_argument("--count-test-episodes", type=int, default=100)
    parser.add_argument(
        "--sensors",
        type=str,
        default="RGB_SENSOR,DEPTH_SENSOR",
        help="comma separated string containing different"
        "sensors to use, currently 'RGB_SENSOR' and"
        "'DEPTH_SENSOR' are supported",
    )
    parser.add_argument(
        "--task-config",
        type=str,
        default="configs/tasks/pointnav.yaml",
        help="path to config yaml containing information about task",
    )
    parser.add_argument(
        "--video-option",
        type=str,
        default="",
        choices=["tensorboard", "disk"],
        nargs="*",
        help="Options for video output, leave empty for no video. "
        "Videos can be saved to disk, uploaded to tensorboard, or both.",
    )
    parser.add_argument(
        "--video-dir", type=str, help="directory for storing videos"
    )
    parser.add_argument(
        "--tensorboard-dir",
        type=str,
        help="directory for storing tensorboard statistics",
    )

    args = parser.parse_args()

    assert (args.model_path is not None) != (
        args.tracking_model_dir is not None
    ), "Must specify a single model or a directory of models, but not both"
    if "tensorboard" in args.video_option:
        assert (
            args.tensorboard_dir is not None
        ), "Must specify a tensorboard directory for video display"
    if "disk" in args.video_option:
        assert (
            args.video_dir is not None
        ), "Must specify a directory for storing videos on disk"

    with get_tensorboard_writer(
        args.tensorboard_dir, purge_step=0, flush_secs=30
    ) as writer:
        if args.model_path is not None:
            # evaluate singe checkpoint
            eval_checkpoint(args.model_path, args, writer)
        else:
            # evaluate multiple checkpoints in order
            prev_ckpt_ind = -1
            while True:
                current_ckpt = None
                while current_ckpt is None:
                    current_ckpt = poll_checkpoint_folder(
                        args.tracking_model_dir, prev_ckpt_ind
                    )
                    time.sleep(2)  # sleep for 2 seconds before polling again
                logger.warning(
                    "=============current_ckpt: {}=============".format(
                        current_ckpt
                    )
                )
                prev_ckpt_ind += 1
                eval_checkpoint(
                    current_ckpt, args, writer, cur_ckpt_idx=prev_ckpt_ind
                )


if __name__ == "__main__":
    main()
