import glob
import os
import time

from absl import app
import gin
from internal import configs
from internal import datasets
from internal import models
from internal import train_utils
from internal import checkpoints
from internal import utils
from matplotlib import cm
import mediapy as media
import torch
import numpy as np
import accelerate
import imageio
from torch.utils._pytree import tree_map

configs.define_common_flags()


def create_videos(config, base_dir, out_dir, out_name, num_frames):
    """Creates videos out of the images saved to disk."""
    names = [n for n in config.exp_path.split('/') if n]
    # Last two parts of checkpoint path are experiment name and scene name.
    exp_name, scene_name = names[-2:]
    video_prefix = f'{scene_name}_{exp_name}_{out_name}'

    zpad = max(3, len(str(num_frames - 1)))
    idx_to_str = lambda idx: str(idx).zfill(zpad)

    utils.makedirs(base_dir)

    # Load one example frame to get image shape and depth range.
    depth_file = os.path.join(out_dir, f'distance_mean_{idx_to_str(0)}.tiff')
    depth_frame = utils.load_img(depth_file)
    shape = depth_frame.shape
    p = config.render_dist_percentile
    distance_limits = np.percentile(depth_frame.flatten(), [p, 100 - p])
    lo, hi = [config.render_dist_curve_fn(x) for x in distance_limits]
    print(f'Video shape is {shape[:2]}')

    for k in ['color', 'normals', 'acc', 'distance_mean', 'distance_median']:
        video_file = os.path.join(base_dir, f'{video_prefix}_{k}.mp4')
        input_format = 'gray' if k == 'acc' else 'rgb'
        file_ext = 'png' if k in ['color', 'normals'] else 'tiff'
        idx = 0
        file0 = os.path.join(out_dir, f'{k}_{idx_to_str(0)}.{file_ext}')
        if not utils.file_exists(file0):
            print(f'Images missing for tag {k}')
            continue
        print(f'Making video {video_file}...')

        writer = imageio.get_writer(video_file, fps=config.render_video_fps)
        for idx in range(num_frames):
            img_file = os.path.join(out_dir, f'{k}_{idx_to_str(idx)}.{file_ext}')
            if not utils.file_exists(img_file):
                ValueError(f'Image file {img_file} does not exist.')

            img = utils.load_img(img_file)
            if k in ['color', 'normals']:
                img = img / 255.
            elif k.startswith('distance'):
                img = config.render_dist_curve_fn(img)
                img = np.clip((img - np.minimum(lo, hi)) / np.abs(hi - lo), 0, 1)
                img = cm.get_cmap('turbo')(img)[..., :3]

            frame = (np.clip(np.nan_to_num(img), 0., 1.) * 255.).astype(np.uint8)
            writer.append_data(frame)
        writer.close()

num_class = 19
def def_color_map():
    s = 256**3//num_class
    colormap = [[(i*s)//(256**2),((i*s)//(256)%256),(i*s)%(256) ] for i in range(num_class)]
    return colormap

color_map = np.array(def_color_map())

def visualize_depth(depth, near=0.2, far=13):
    colormap = cm.get_cmap('turbo')
    curve_fn = lambda x: -np.log(x + np.finfo(np.float32).eps)
    eps = np.finfo(np.float32).eps
    near = near if near else depth.min()
    far = far if far else depth.max()
    near -= eps
    far += eps
    near, far, depth = [curve_fn(x) for x in [near, far, depth]]
    depth = np.nan_to_num(
        np.clip((depth - np.minimum(near, far)) / np.abs(far - near), 0, 1))
    vis = colormap(depth)[:, :, :3]
    # os.makedirs('./test_depths/'+pathname,exist_ok=True)
    out_depth = np.clip(np.nan_to_num(vis), 0., 1.) * 255
    return out_depth

def main(unused_argv):
    config = configs.load_config()
    config.exp_path = os.path.join('exp', config.exp_name)
    config.render_dir = os.path.join(config.exp_path, 'render')

    accelerator = accelerate.Accelerator()
    config.world_size = accelerator.num_processes
    config.local_rank = accelerator.local_process_index

    dataset = datasets.load_dataset('test', config.data_dir, config)
    if dataset.semantics is not None:
        config.use_semantic = True
    else:
        config.use_semantic = False

    utils.seed_everything(config.seed + accelerator.local_process_index)
    config.analytic_gradient = False
    model = models.Model(config=config,bboxes = dataset.bboxes)
    # model.nerf_mlp.disable_density_normals = False # rendering normals

    step = checkpoints.restore_checkpoint(config.exp_path, model)
    accelerator.print(f'Rendering checkpoint at step {step}.')
    model.to(accelerator.device)


    dataloader = torch.utils.data.DataLoader(np.arange(len(dataset)),
                                             num_workers=0,
                                             shuffle=False,
                                             batch_size=1,
                                             collate_fn=dataset.collate_fn,
                                             )
    dataiter = iter(dataloader)
    if config.rawnerf_mode:
        postprocess_fn = dataset.metadata['postprocess_fn']
    else:
        postprocess_fn = lambda z: z

    out_name = 'path_renders' if config.render_path else 'test_preds'
    out_name = f'{out_name}_step_{step}'
    out_dir = os.path.join(config.render_dir, out_name)
    if not utils.isdir(out_dir):
        utils.makedirs(out_dir)

    path_fn = lambda x: os.path.join(out_dir, x)

    

    # Ensure sufficient zero-padding of image indices in output filenames.
    zpad = max(3, len(str(dataset.size - 1)))
    idx_to_str = lambda idx: str(idx).zfill(zpad)

    for idx in range(dataset.size):
        # If current image and next image both already exist, skip ahead.
        idx_str = idx_to_str(idx)
        curr_file = path_fn(f'color_{idx_str}.png')
        # if utils.file_exists(curr_file):
        #     accelerator.print(f'Image {idx + 1}/{dataset.size} already exists, skipping')
        #     continue
        batch = next(dataiter)
        batch = tree_map(lambda x: x.to(accelerator.device) if x is not None else None, batch)
        accelerator.print(f'Evaluating image {idx + 1}/{dataset.size}')
        eval_start_time = time.time()
        rendering = models.render_image(
            lambda rand, x: model(rand,
                                  x,
                                  train_frac=1.,
                                  compute_extras=True,
                                  sample_n=config.sample_n_test,
                                  sample_m=config.sample_m_test,
                                  ),
            accelerator,
            batch, False, config)

        accelerator.print(f'Rendered in {(time.time() - eval_start_time):0.3f}s')

        if accelerator.is_local_main_process:  # Only record via host 0.
            rendering['rgb'] = postprocess_fn(rendering['rgb'])
            rendering = tree_map(lambda x: x.detach().cpu().numpy() if x is not None else None, rendering)
            utils.save_img_u8(rendering['rgb'], path_fn(f'color_{idx_str}.png'))
            if 'normals' in rendering:
                norm = rendering['normals'] 
                # camera_pose = dataset.poses[idx]
                # H , W = rendering['rgb'].shape[:2]
                # norm = np.matmul(np.linalg.inv(camera_pose[:3,:3])[None,:,:] , norm.reshape(-1,3,1)).reshape(H,W,3)
                normals = norm/ 2. + 0.5
                utils.save_img_u8(normals,
                                  path_fn(f'normals_{idx_str}.png'))
            # utils.save_img_f32(rendering['distance_mean'],
            #                    path_fn(f'distance_mean_{idx_str}.tiff'))
            # utils.save_img_f32(rendering['distance_median'],
            #                    path_fn(f'distance_median_{idx_str}.tiff'))
            # utils.save_img_f32(rendering['acc'], path_fn(f'acc_{idx_str}.tiff'))
            dep = visualize_depth(rendering['depth'])
            from PIL import Image
            Image.fromarray(dep.astype(np.uint8)).save(path_fn(f'dep_{idx_str}.png'))
            logits_2_label = lambda x: np.argmax(x, axis=-1)
            labels = logits_2_label(rendering['semantic'])
            labels = color_map[labels]
            Image.fromarray(labels.astype(np.uint8)).save(path_fn(f'sem_{idx_str}.png'))


    num_files = len(glob.glob(path_fn('acc_*.tiff')))
    if accelerator.is_local_main_process and num_files == dataset.size:
        accelerator.print(f'All files found, creating videos).')
        create_videos(config, config.render_dir, out_dir, out_name, dataset.size)
    accelerator.wait_for_everyone()


if __name__ == '__main__':
    with gin.config_scope('eval'):  # Use the same scope as eval.py
        app.run(main)
