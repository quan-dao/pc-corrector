import torch

from .detector3d_template import Detector3DTemplate
from _dev_space.tail_cutter import PointAligner
from _dev_space.tail_cutter_heads import AlignerHead
import logging


class Aligner(Detector3DTemplate):
    def __init__(self, model_cfg, num_class, dataset):
        super().__init__(model_cfg=model_cfg, num_class=num_class, dataset=dataset)
        self.aligner = PointAligner(model_cfg)
        if model_cfg.get('FREEZE_1ST_STAGE', False):
            self.aligner.eval()
            for param in self.aligner.parameters():
                param.requires_grad = False

        self.det_head = AlignerHead(model_cfg.ALIGNER_HEAD, self.aligner.backbone2d.n_output_feat,
                                    self.aligner.num_instance_features)

    def forward(self, batch_dict):
        if self.model_cfg.get('FREEZE_1ST_STAGE', False):
            self.aligner.eval()
            with torch.no_grad():
                batch_dict = self.aligner(batch_dict)
        else:
            batch_dict = self.aligner(batch_dict)

        batch_dict = self.det_head(batch_dict)

        if self.training:
            tb_dict = dict()
            if self.aligner.training:
                loss_aligner, tb_dict = self.aligner.get_training_loss(batch_dict)

            loss_head, tb_dict = self.det_head.get_training_loss(tb_dict)

            if self.aligner.training:
                loss = loss_aligner + loss_head
            else:
                loss = loss_head

            ret_dict = {'loss': loss}
            tb_dict['loss_total'] = loss.item()
            if self.model_cfg.get('DEBUG', False):
                logger = logging.getLogger()
                logger.warning('during training, DEBUG flag is on, return batch_dict together with ret_dict & tb_dict')
                return ret_dict, tb_dict, {}, batch_dict
            else:
                return ret_dict, tb_dict, {}
        else:
            if self.model_cfg.get('DEBUG', False):
                logger = logging.getLogger()
                logger.warning('DEBUG flag is on, migh not generate boxes')
                return batch_dict
            else:
                pred_dicts, recall_dicts = self.post_processing(batch_dict)
                return pred_dicts, recall_dicts

    def post_processing(self, batch_dict):
        post_process_cfg = self.model_cfg.POST_PROCESSING
        batch_size = batch_dict['batch_size']
        final_pred_dict = batch_dict['final_box_dicts']
        recall_dict = {}
        for index in range(batch_size):
            pred_boxes = final_pred_dict[index]['pred_boxes']

            recall_dict = self.generate_recall_record(
                box_preds=pred_boxes,
                recall_dict=recall_dict, batch_index=index, data_dict=batch_dict,
                thresh_list=post_process_cfg.RECALL_THRESH_LIST
            )

        return final_pred_dict, recall_dict
