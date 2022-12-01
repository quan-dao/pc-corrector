import numpy as np
import torch
import torch.nn as nn
from einops import rearrange
import torch_scatter
from typing import List
import logging


class AlignerHead(nn.Module):
    def __init__(self, model_cfg, num_instance_features: int):
        super().__init__()
        self.cfg = model_cfg
        attn_cfg = model_cfg.ATTENTION_STACK
        decoder_cfg = model_cfg.DECODER

        self.embed_local_feat = nn.Linear(num_instance_features + 6, attn_cfg.EMBED_DIM)
        # +6 because of position encoding made of [x, y, yaw, v_x, v_y, v_yaw]
        self.local_self_attention_stack = nn.ModuleList([
            nn.MultiheadAttention(attn_cfg.EMBED_DIM, attn_cfg.LOCAL_NUM_HEADS, dropout=0.1)
            for _ in range(attn_cfg.LOCAL_NUM_LAYERS)
        ])

        self.embed_global_feat = nn.Linear(num_instance_features, attn_cfg.EMBED_DIM)
        self.global_cross_attention_stack = nn.ModuleList([
            nn.MultiheadAttention(attn_cfg.EMBED_DIM, attn_cfg.GLOBAL_NUM_HEADS, dropout=0.1)
            for _ in range(attn_cfg.GLOBAL_NUM_LAYERS)
        ])

        self.obj_decoder = self._make_mlp(attn_cfg.EMBED_DIM, 8, decoder_cfg.get('OBJECT_HIDDEN_CHANNELS', []))
        # following residual coder: delta_x, delta_y, delta_z, delta_dx, delta_dy, delta_dz, diff_sin, diff_cos

        self.traj_decoder = self._make_mlp(attn_cfg.EMBED_DIM, 3 * decoder_cfg.TRAJECTORY_NUM_WAYPOINTS,
                                           decoder_cfg.get('TRAJECTORY_HIDDEN_CHANNELS', []))

        self.forward_return_dict = dict()

    @staticmethod
    def _make_mlp(channels_in: int, channels_out: int, channels_hidden: List[int] = None):
        """
        Args:
            channels_in: number of channels of input
            channels: [Hidden_0, ..., Hidden_N, Out]
        """
        if channels_hidden is None:
            channels_hidden = []
        channels = [channels_in] + channels_hidden + [channels_out]
        layers = []
        for c_idx in range(len(channels) - 1):
            is_last_layer = c_idx == (len(channels) - 2)
            layers.append(nn.Linear(channels[c_idx], channels[c_idx + 1], bias=is_last_layer))
            if not is_last_layer:
                layers.append(nn.BatchNorm1d(channels[c_idx + 1], eps=1e-3, momentum=0.01))
                layers.append(nn.ReLU(True))
        return nn.Sequential(*layers)

    def get_training_loss(self, tb_dict: dict = None):
        if tb_dict is None:
            tb_dict = dict()

        loss_box_refine = nn.functional.smooth_l1_loss(
            self.forward_return_dict['obj']['pred_boxes_refinement'],
            self.forward_return_dict['obj']['target_boxes_refinement'],
            reduction='mean'
        )
        tb_dict['loss_box_refine'] = loss_box_refine.item()
        return loss_box_refine, tb_dict

    @torch.no_grad()
    def assign_target_object(self, batch_dict):
        input_dict = batch_dict['input_2nd_stage']
        pred_boxes = input_dict['pred_boxes']  # (N_inst, 7) - center (3), size (3), yaw

        gt_boxes = batch_dict['gt_boxes']  # (B, N_inst_max, 11) -center (3), size (3), yaw, dummy_v (2), instance_index, class
        gt_boxes = rearrange(gt_boxes, 'B N_inst_max C -> (B N_inst_max) C')
        gt_boxes = gt_boxes[input_dict['meta']['inst_bi']]  # (N_inst, 11)

        # map gt_boxes to pred_boxes
        pred_cos, pred_sin = torch.cos(pred_boxes[:, -1]), torch.sin(pred_boxes[:, -1])
        zeros, ones = pred_boxes.new_zeros(pred_boxes.shape[0]), pred_boxes.new_ones(pred_boxes.shape[0])
        pred_rot_inv = rearrange([
            pred_cos,    pred_sin,  zeros,
            -pred_sin,   pred_cos,  zeros,
            zeros,       zeros,     ones
        ], '(r c) N_inst -> N_inst r c', r=3, c=3)

        transl_gt_in_pred = torch.einsum('nij, nj -> ni', pred_rot_inv, gt_boxes[:, :3] - pred_boxes[:, :3])

        # residual coder
        diagonal = torch.sqrt(torch.square(pred_boxes[:, 3: 5]).sum(dim=1, keepdim=True))
        target_transl_xy = transl_gt_in_pred[:, :2] / torch.clamp_min(diagonal, min=1e-2)  # (N_inst, 2)
        target_transl_z = rearrange(transl_gt_in_pred[:, 2] / torch.clamp_min(pred_boxes[:, 5], min=1e-2),
                                    'N_inst -> N_inst 1')
        target_size = torch.log(gt_boxes[:, 3: 6] / torch.clamp_min(pred_boxes[:, 3: 6], min=1e-2))  # (N_inst, 3)
        target_ori = torch.stack([
            torch.cos(-pred_boxes[:, -1] + gt_boxes[:, 6]), torch.sin(-pred_boxes[:, -1] + gt_boxes[:, 6])
        ], dim=1)  # (N_inst, 2) - (cos, sin)

        target_boxes_refinement = torch.cat([target_transl_xy, target_transl_z, target_size, target_ori], dim=1)
        return target_boxes_refinement

    @staticmethod
    @torch.no_grad()
    def decode_boxes_refinement(boxes: torch.Tensor, refinement: torch.Tensor):
        """
        Reconstruct rot_gt_in_pred & transl_gt_in_pred -> ^{pred} T_{pred'}
        """
        diagonal = torch.sqrt(torch.square(boxes[:, 3: 5]).sum(dim=1, keepdim=True))
        transl_xy = refinement[:, :2] * diagonal  # (N_inst, 2)
        transl_z = rearrange(refinement[:, 2] * boxes[:, 5], 'N_inst -> N_inst 1')
        transl = torch.cat([transl_xy, transl_z], dim=1)

        cos, sin = torch.cos(boxes[:, 6]), torch.sin(boxes[:, 6])
        zeros, ones = boxes.new_zeros(boxes.shape[0]), boxes.new_ones(boxes.shape[0])
        rot_box = rearrange([
            cos,     -sin,   zeros,
            sin,      cos,   zeros,
            zeros,  zeros,   ones
        ], '(r c) N_inst -> N_inst r c', r=3, c=3)

        new_xyz = torch.einsum('nij, nj -> ni', rot_box, transl) + boxes[:, :3]

        new_yaw = boxes[:, 6] + torch.atan2(refinement[:, -1], refinement[:, -2])
        new_yaw = torch.atan2(torch.sin(new_yaw), torch.cos(new_yaw))

        new_sizes = torch.exp(refinement[:, 3: 6]) * boxes[:, 3: 6]

        return torch.cat([new_xyz, new_sizes, rearrange(new_yaw, 'N_inst -> N_inst 1')], dim=1)

    @torch.no_grad()
    def generate_predicted_boxes(self, batch_size: int, input_dict) -> List[dict]:
        """
        Element i-th of the returned List represents predicted boxes for the sample i-th in the batch
        Args:
            batch_size:
            input_dict
        """
        # decode predicted proposals
        pred_boxes = self.decode_boxes_refinement(input_dict['pred_boxes'],
                                                  self.forward_return_dict['obj']['pred_boxes_refinement'])  # (N_inst, 7)
        if self.cfg.get('DEBUG_NOT_APPLYING_REFINEMENT', False):
            # logger = logging.getLogger()
            # logger.info('NOT USING REFINEMENT, showing pred_boxes made by 1st stage')
            pred_boxes = input_dict['pred_boxes']

        # compute boxes' score by weighted sum foreground score, weight come from attention matrix
        # attn_weights =self.forward_return_dict['obj']['attn_weights']  # (N_fg)
        pred_scores = torch_scatter.scatter_mean(input_dict['foreground']['fg_prob'],
                                                 input_dict['meta']['inst_bi_inv_indices'])  # (N_inst,)

        # separate pred_boxes accodring to batch index
        inst_bi = input_dict['meta']['inst_bi']
        max_num_inst = input_dict['meta']['max_num_inst']
        inst_batch_idx = inst_bi // max_num_inst  # (N_inst,)

        out = []
        for b_idx in range(batch_size):
            cur_boxes = pred_boxes[inst_batch_idx == b_idx]
            cur_scores = pred_scores[inst_batch_idx == b_idx]  # (N_cur_inst,)
            cur_labels = cur_boxes.new_ones(cur_boxes.shape[0]).long()
            out.append({
                'pred_boxes': cur_boxes,
                'pred_scores': cur_scores,
                'pred_labels': cur_labels
            })
        return out

    @torch.no_grad()
    def gen_instances_past_trajectory(self, input_dict: dict, instance_current_boxes: torch.Tensor):
        """
        Args:
            input_dict:
            instance_current_boxes: (N_inst, 7) - x, y, z, dx, dy, dz, yaw
        Returns:
            (N_local, 4) - x, y, z, yaw
        """
        # extract local_tf
        local_transl = input_dict['local']['local_transl']  # (N_local, 3)
        local_rot = input_dict['local']['local_rot']  # (N_local, 3, 3)
        local_rot_angle = torch.atan2(local_rot[:, 1, 0], local_rot[:, 0, 0])  # (N_local,) - assume only have rot around x

        # reconstruct local poses
        indices_inst2local = input_dict['meta']['local_bi_in_inst_bi']  # (N_local,)
        local_yaw = -local_rot_angle + instance_current_boxes[indices_inst2local, -1]  # (N_local,)

        offset = instance_current_boxes[indices_inst2local, :3] - local_transl  # (N_local, 3)
        cos, sin = torch.cos(-local_rot_angle), torch.sin(-local_rot_angle)  # (N_local,)
        local_xyz = torch.stack([
            cos * offset[:, 0] - sin * offset[:, 1],
            sin * offset[:, 0] + cos * offset[:, 1],
            offset[:, 2]
        ], dim=1)  # (N_local, 3)

        return torch.cat([local_xyz, local_yaw[:, None]], dim=1)  # (N_local, 4)

    def forward(self, batch_dict):
        input_dict = batch_dict['input_2nd_stage']

        # unpack meta stuff
        indices_inst2local = input_dict['meta']['local_bi_in_inst_bi']  # (N_local,)
        num_locals_in_instances = input_dict['meta']['num_locals_in_instances']  # (N_inst,)
        inst_bi = input_dict['meta']['inst_bi']  # (N_inst,)
        num_instances = inst_bi.shape[0]
        max_locals_per_instance = num_locals_in_instances.max()

        if self.training:
            inst_cur_boxes = input_dict['pred_boxes']  # (N_inst, 7) - x, y, z, dx, dy, dz, yaw
        else:
            inst_cur_boxes = self.decode_boxes_refinement(
                input_dict['pred_boxes'], self.forward_return_dict['pred']['boxes_refinement']
            )  # (N_inst, 7) - x, y, z, dx, dy, dz, yaw

        local_pose = self.gen_instances_past_trajectory(input_dict, inst_cur_boxes)  # (N_local, 4) - x,y,z,yaw in global
        local_pose = local_pose[:, [0, 1, 3]]  # (N_local, 3) - x, y, yaw (exclude z) in global

        # map local_pose to instance frame
        # translate to origin of instance current frame
        local_pose[:, :2] = local_pose[:, :2] - inst_cur_boxes[indices_inst2local, :2]
        # rotate to instance current frame
        cos, sin = torch.cos(inst_cur_boxes[:, 6]), torch.sin(inst_cur_boxes[:, 6])  # (N_inst)
        cos, sin = cos[indices_inst2local], sin[indices_inst2local]  # (N_local,)
        l_x = cos * local_pose[:, 0] + sin * local_pose[:, 1]  # (N_local,)
        l_y = -sin * local_pose[:, 0] + sin * local_pose[:, 1]  # (N_local,)
        local_pose[:, 2] = local_pose[:, 2] - inst_cur_boxes[:, 2]  # (N_local,)
        # overwrite x,y-coord of local pose & put yaw of local pose in range [-pi, pi)
        local_pose[:, :2] = torch.stack([l_x, l_y], dim=1)
        local_pose[:, 2] = torch.atan2(torch.sin(local_pose[:, 2]), torch.cos(local_pose[:, 2]))

        # compute locals' velocity
        local_sweep_idx = input_dict['meta']['local_bisw'] % self.cfg('NUM_SWEEPS')  # (N_local,)
        inst_max_sweep_idx = input_dict['meta']['inst_max_sweep_idx']  # (N_inst)
        local_time_elapsed = (inst_max_sweep_idx[indices_inst2local] - local_sweep_idx) * 0.05  # (N_local,)
        # * 0.1 because sweeps are produced at 20Hz
        assert torch.all(local_time_elapsed > -1e-5)
        local_velo = torch.zeros_like(local_pose)
        mask_valid_time = local_time_elapsed > 1e-5  # (N_local,)
        local_velo[mask_valid_time] = local_pose[mask_valid_time] / local_time_elapsed[mask_valid_time]

        # compute instance velocity to use as velocity for locals that have invalid time
        inst_velo = torch_scatter.scatter_sum(local_velo, indices_inst2local, dim=0) / \
                    torch.clamp_min(num_locals_in_instances - 1, min=1.0)
        # - 1 because velo of the most recent locals == 0

        local_velo = local_velo + inst_velo[indices_inst2local] * torch.logical_not(mask_valid_time).float()

        # construct local feats for attention
        local_feat = torch.cat(
            [input_dict['local']['features'], local_pose, local_velo], dim=1)  # (N_local, C_inst + 6)

        batch_local_feat = local_feat.new_zeros(num_instances, max_locals_per_instance, local_feat.shape[-1])
        key_padding_mask = local_feat.new_zeros(num_instances, max_locals_per_instance)
        for instance_idx in range(num_instances):
            batch_local_feat[instance_idx, :num_locals_in_instances[instance_idx]] = \
                local_feat[indices_inst2local == instance_idx]
            # indices_inst2local[i] = j means local i-th is associated with instance j-th
            # (+) i in range(N_local)
            # (+) j in range(N_inst)
            key_padding_mask[instance_idx, num_locals_in_instances[instance_idx]:] = 1  # to be ignored by attention

        batch_local_feat = rearrange(batch_local_feat, 'N_inst N_local_max C -> N_local_max N_inst C').contiguous()
        # to be compatible with setting batch_first=False on torch 1.8.2

        # self-attention to refine local feat
        batch_local_feat = self.embed_local_feat(batch_local_feat)  # (N_local_max, N_inst, C_attn)
        for self_attn in self.local_self_attention_stack:
            batch_local_feat, _ = self_attn(batch_local_feat, batch_local_feat, batch_local_feat,
                                            key_padding_mask=key_padding_mask)  # (N_local_max, N_inst, C_attn)

        # cross attention to get global feat
        batch_global_feat = rearrange(input_dict['global']['features'], 'N_inst C_inst -> 1 N_inst C_inst').contiguous()
        batch_global_feat = self.embed_global_feat(batch_global_feat)  # (1, N_inst, C_attn)
        for cross_attn in self.global_cross_attention_stack:
            batch_global_feat, _ = cross_attn(batch_global_feat, batch_local_feat, batch_local_feat,
                                              key_padding_mask=key_padding_mask)  # (1, N_inst, C_attn)
        batch_global_feat = rearrange(batch_global_feat, '1 N_inst C_attn -> N_inst C_attn')

        # decode batch_global_feat to get box refinement & future trajectories
        pred_boxes_refine = self.obj_decoder(batch_global_feat)  # (N_inst, 8)
        pred_trajectories = self.traj_decoder(batch_global_feat)  # (N_inst, 3 * num_waypts)
        self.forward_return_dict['pred'] = {
            'boxes_refine': pred_boxes_refine,
            'trajectories': pred_trajectories
        }

    def assign_target_trajectory(self, batch_dict):
        pass  # TODO
