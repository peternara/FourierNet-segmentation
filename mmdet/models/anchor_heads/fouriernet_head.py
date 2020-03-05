import torch
import torch.nn as nn
from mmcv.cnn import normal_init
from torch import Tensor

from mmdet.core import distance2bbox, force_fp32, multi_apply, multiclass_nms_with_mask
from mmdet.ops import ModulatedDeformConvPack

from ..builder import build_loss
from ..registry import HEADS
from ..utils import ConvModule, Scale, bias_init_with_prob, build_norm_layer
import math

INF = 1e8


@HEADS.register_module
class FourierNetHead(nn.Module):

    def __init__(self,
                 num_classes,
                 in_channels,
                 feat_channels=256,
                 stacked_convs=4,
                 strides=(4, 8, 16, 32, 64),
                 regress_ranges=((-1, 64), (64, 128), (128, 256), (256, 512),
                                 (512, INF)),
                 use_dcn=False,
                 mask_nms=False,
                 bbox_from_mask=False,
                 loss_cls=None,
                 loss_bbox=None,
                 loss_mask=None,
                 loss_on_coe=False,
                 loss_centerness=None,
                 conv_cfg=None,
                 norm_cfg=None,
                 contour_points=360,
                 use_fourier=False,
                 num_coe=36,
                 visulize_coe=36,
                 centerness_factor=0.5):
        super(FourierNetHead, self).__init__()
        self.use_fourier = use_fourier
        self.contour_points = contour_points
        self.num_coe = num_coe
        self.visulize_coe = visulize_coe
        self.interval = 360 // self.contour_points
        self.num_classes = num_classes
        self.cls_out_channels = num_classes - 1
        self.in_channels = in_channels
        self.feat_channels = feat_channels
        self.stacked_convs = stacked_convs
        self.strides = strides
        self.regress_ranges = regress_ranges
        self.loss_cls = build_loss(loss_cls)
        self.loss_bbox = build_loss(loss_bbox)
        self.loss_mask = build_loss(loss_mask)
        self.loss_on_coe = loss_on_coe
        self.loss_centerness = build_loss(loss_centerness)
        self.conv_cfg = conv_cfg
        self.norm_cfg = norm_cfg
        self.fp16_enabled = False
        self.use_dcn = use_dcn
        self.mask_nms = mask_nms
        self.bbox_from_mask = bbox_from_mask
        self.vis_num = 1000
        self.count = 0
        self.angles = torch.range(0, 359, self.interval).cuda() / 180 * math.pi
        self.centerness_factor = centerness_factor
        self._init_layers()

    def _init_layers(self):
        self.cls_convs = nn.ModuleList()
        if not self.bbox_from_mask:
            self.reg_convs = nn.ModuleList()
        self.mask_convs = nn.ModuleList()
        for i in range(self.stacked_convs):
            chn = self.in_channels if i == 0 else self.feat_channels
            if not self.use_dcn:
                self.cls_convs.append(
                    ConvModule(
                        chn,
                        self.feat_channels,
                        3,
                        stride=1,
                        padding=1,
                        conv_cfg=self.conv_cfg,
                        norm_cfg=self.norm_cfg,
                        bias=self.norm_cfg is None))
                if not self.bbox_from_mask:
                    self.reg_convs.append(
                        ConvModule(
                            chn,
                            self.feat_channels,
                            3,
                            stride=1,
                            padding=1,
                            conv_cfg=self.conv_cfg,
                            norm_cfg=self.norm_cfg,
                            bias=self.norm_cfg is None))
                self.mask_convs.append(
                    ConvModule(
                        chn,
                        self.feat_channels,
                        3,
                        stride=1,
                        padding=1,
                        conv_cfg=self.conv_cfg,
                        norm_cfg=self.norm_cfg,
                        bias=self.norm_cfg is None))
            else:
                self.cls_convs.append(
                    ModulatedDeformConvPack(
                        chn,
                        self.feat_channels,
                        3,
                        stride=1,
                        padding=1,
                        dilation=1,
                        deformable_groups=1,
                    ))
                if self.norm_cfg:
                    self.cls_convs.append(build_norm_layer(self.norm_cfg, self.feat_channels)[1])
                self.cls_convs.append(nn.ReLU(inplace=True))

                if not self.bbox_from_mask:
                    self.reg_convs.append(
                        ModulatedDeformConvPack(
                            chn,
                            self.feat_channels,
                            3,
                            stride=1,
                            padding=1,
                            dilation=1,
                            deformable_groups=1,
                        ))
                    if self.norm_cfg:
                        self.reg_convs.append(build_norm_layer(self.norm_cfg, self.feat_channels)[1])
                    self.reg_convs.append(nn.ReLU(inplace=True))

                self.mask_convs.append(
                    ModulatedDeformConvPack(
                        chn,
                        self.feat_channels,
                        3,
                        stride=1,
                        padding=1,
                        dilation=1,
                        deformable_groups=1,
                    ))
                if self.norm_cfg:
                    self.mask_convs.append(build_norm_layer(self.norm_cfg, self.feat_channels)[1])
                self.mask_convs.append(nn.ReLU(inplace=True))

        self.polar_cls = nn.Conv2d(
            self.feat_channels, self.cls_out_channels, 3, padding=1)
        self.polar_reg = nn.Conv2d(self.feat_channels, 4, 3, padding=1)
        if self.use_fourier:
            self.polar_mask = nn.Conv2d(self.feat_channels, self.num_coe * 2, 3, padding=1)
        else:
            self.polar_mask = nn.Conv2d(self.feat_channels, self.contour_points, 3, padding=1)
        self.polar_centerness = nn.Conv2d(self.feat_channels, 1, 3, padding=1)

        self.scales_bbox = nn.ModuleList([Scale(1.0) for _ in self.strides])
        self.scales_mask = nn.ModuleList([Scale(1.0) for _ in self.strides])

    def init_weights(self):
        if not self.use_dcn:
            for m in self.cls_convs:
                normal_init(m.conv, std=0.01)
            if not self.bbox_from_mask:
                for m in self.reg_convs:
                    normal_init(m.conv, std=0.01)
            for m in self.mask_convs:
                normal_init(m.conv, std=0.01)
        else:
            pass

        bias_cls = bias_init_with_prob(0.01)
        normal_init(self.polar_cls, std=0.01, bias=bias_cls)
        normal_init(self.polar_reg, std=0.01)
        normal_init(self.polar_mask, std=0.01)
        normal_init(self.polar_centerness, std=0.01)

    def forward(self, feats):
        return multi_apply(self.forward_single, feats, self.scales_bbox, self.scales_mask)

    def forward_single(self, x, scale_bbox, scale_mask):
        cls_feat = x
        reg_feat = x
        mask_feat = x

        for cls_layer in self.cls_convs:
            cls_feat = cls_layer(cls_feat)
        cls_score = self.polar_cls(cls_feat)

        for mask_layer in self.mask_convs:
            mask_feat = mask_layer(mask_feat)
        if self.use_fourier:
            mask_pred = self.polar_mask(mask_feat)
            mask_pred = scale_mask(mask_pred)
        else:
            mask_pred = scale_mask(self.polar_mask(mask_feat)).float().exp()

        centerness = self.polar_centerness(cls_feat)

        if not self.bbox_from_mask:
            for reg_layer in self.reg_convs:
                reg_feat = reg_layer(reg_feat)
            # scale the bbox_pred of different level
            # float to avoid overflow when enabling FP16
            bbox_pred = scale_bbox(self.polar_reg(reg_feat)).float().exp()
        else:
            bbox_pred = mask_pred[:, :4, :, :]

        return cls_score, bbox_pred, centerness, mask_pred

    @force_fp32(apply_to=('cls_scores', 'bbox_preds', 'mask_preds', 'centernesses'))
    def loss(self,
             cls_scores,
             bbox_preds,
             centernesses,
             mask_preds,
             gt_bboxes,
             gt_labels,
             img_metas,
             cfg,
             gt_masks,
             gt_bboxes_ignore=None,
             extra_data=None):
        assert len(cls_scores) == len(bbox_preds) == len(centernesses) == len(mask_preds)
        featmap_sizes = [featmap.size()[-2:] for featmap in cls_scores]
        all_level_points = self.get_points(featmap_sizes, bbox_preds[0].dtype,
                                           bbox_preds[0].device)

        labels, bbox_targets, mask_targets, centerness_targets = self.polar_target(all_level_points, extra_data)

        num_imgs = cls_scores[0].size(0)
        # flatten cls_scores, bbox_preds and centerness
        flatten_cls_scores = [
            cls_score.permute(0, 2, 3, 1).reshape(-1, self.cls_out_channels)
            for cls_score in cls_scores]

        flatten_centerness = [
            centerness.permute(0, 2, 3, 1).reshape(-1)
            for centerness in centernesses
        ]
        if self.use_fourier:
            if self.loss_on_coe:
                flatten_mask_preds = [
                    mask_pred.permute(0, 2, 3, 1).reshape(-1, self.num_coe, 2)
                    for mask_pred in mask_preds
                ]
            else:
                flatten_mask_preds = []
                flatten_bbox_preds = []
                for mask_pred, points in zip(mask_preds, all_level_points):
                    mask_pred = mask_pred.permute(0, 2, 3, 1).reshape(-1, self.num_coe, 2)
                    if self.bbox_from_mask:
                        xy, m = self.distance2mask(points.repeat(num_imgs, 1), mask_pred, train=True)
                        b = torch.stack([xy[:, 0].min(1)[0],
                                         xy[:, 1].min(1)[0],
                                         xy[:, 0].max(1)[0],
                                         xy[:, 1].max(1)[0]], -1)
                        flatten_bbox_preds.append(b)
                        flatten_mask_preds.append(m)
                    else:
                        m = torch.irfft(torch.cat([mask_pred, torch.zeros(mask_pred.shape[0],
                                                                          self.contour_points - self.num_coe, 2).to(
                            "cuda")], 1), 1, True, False).float().exp()
                        flatten_mask_preds.append(m)

        else:
            flatten_mask_preds = [
                mask_pred.permute(0, 2, 3, 1).reshape(-1, self.contour_points)
                for mask_pred in mask_preds
            ]
        if not self.bbox_from_mask:
            flatten_bbox_preds = [
                bbox_pred.permute(0, 2, 3, 1).reshape(-1, 4)
                for bbox_pred in bbox_preds
            ]

        flatten_cls_scores = torch.cat(flatten_cls_scores)  # [num_pixel, 80]
        flatten_bbox_preds = torch.cat(flatten_bbox_preds)  # [num_pixel, 4]
        flatten_mask_preds = torch.cat(flatten_mask_preds)  # [num_pixel, 36]
        flatten_centerness = torch.cat(flatten_centerness)  # [num_pixel]

        flatten_labels = torch.cat(labels).long()  # [num_pixel]
        flatten_centerness_targets = torch.cat(centerness_targets)
        flatten_bbox_targets = torch.cat(bbox_targets)  # [num_pixel, 4]
        flatten_mask_targets = torch.cat(mask_targets)  # [num_pixel, 36]
        flatten_points = torch.cat([points.repeat(num_imgs, 1)
                                    for points in all_level_points])  # [num_pixel,2]
        pos_inds = flatten_labels.nonzero().reshape(-1)
        num_pos = len(pos_inds)

        loss_cls = self.loss_cls(
            flatten_cls_scores, flatten_labels,
            avg_factor=num_pos + num_imgs)  # avoid num_pos is 0
        pos_bbox_preds = flatten_bbox_preds[pos_inds]
        pos_centerness = flatten_centerness[pos_inds]
        pos_mask_preds = flatten_mask_preds[pos_inds]

        if num_pos > 0:
            pos_bbox_targets = flatten_bbox_targets[pos_inds]
            pos_mask_targets = flatten_mask_targets[pos_inds]
            pos_centerness_targets = flatten_centerness_targets[pos_inds]

            pos_points = flatten_points[pos_inds]
            if self.bbox_from_mask:
                pos_decoded_bbox_preds = pos_bbox_preds
            else:
                pos_decoded_bbox_preds = distance2bbox(pos_points, pos_bbox_preds)
            pos_decoded_target_preds = distance2bbox(pos_points,
                                                     pos_bbox_targets)

            # centerness weighted iou loss
            loss_bbox = self.loss_bbox(
                pos_decoded_bbox_preds,
                pos_decoded_target_preds,
                weight=pos_centerness_targets,
                avg_factor=pos_centerness_targets.sum())

            if self.loss_on_coe:
                pos_mask_targets = torch.rfft(pos_mask_targets, 1, True, False)
                pos_mask_targets = pos_mask_targets[..., :self.num_coe, :]
                loss_mask = self.loss_mask(pos_mask_preds,
                                           pos_mask_targets)
            else:
                # loss_mask = self.loss_mask(pos_mask_preds,
                #                            pos_mask_targets
                #                            )
                loss_mask = self.loss_mask(pos_mask_preds,
                                           pos_mask_targets,
                                           weight=pos_centerness_targets,
                                           avg_factor=pos_centerness_targets.sum()
                                           )

            loss_centerness = self.loss_centerness(pos_centerness,
                                                   pos_centerness_targets)
        else:
            loss_bbox = pos_bbox_preds.sum()
            loss_mask = pos_mask_preds.sum()
            loss_centerness = pos_centerness.sum()

        return dict(
            loss_cls=loss_cls,
            loss_bbox=loss_bbox,
            loss_mask=loss_mask,
            loss_centerness=loss_centerness)

    def get_points(self, featmap_sizes, dtype, device):
        """Get points according to feature map sizes.

        Args:
            featmap_sizes (list[tuple]): Multi-level feature map sizes.
            dtype (torch.dtype): Type of points.
            device (torch.device): Device of points.

        Returns:
            tuple: points of each image.
        """
        mlvl_points = []
        for i in range(len(featmap_sizes)):
            mlvl_points.append(
                self.get_points_single(featmap_sizes[i], self.strides[i],
                                       dtype, device))
        return mlvl_points

    def get_points_single(self, featmap_size, stride, dtype, device):
        h, w = featmap_size
        x_range = torch.arange(
            0, w * stride, stride, dtype=dtype, device=device)
        y_range = torch.arange(
            0, h * stride, stride, dtype=dtype, device=device)
        y, x = torch.meshgrid(y_range, x_range)
        points = torch.stack(
            (x.reshape(-1), y.reshape(-1)), dim=-1) + stride // 2
        return points

    def polar_target(self, points, extra_data):
        assert len(points) == len(self.regress_ranges)

        num_levels = len(points)

        labels_list, bbox_targets_list, mask_targets_list, centerness_targets_list = extra_data.values()

        # split to per img, per level
        num_points = [center.size(0) for center in points]
        labels_list = [labels.split(num_points, 0) for labels in labels_list]
        centerness_targets_list = [
            centerness_targets.split(num_points, 0)
            for centerness_targets in centerness_targets_list
        ]
        bbox_targets_list = [
            bbox_targets.split(num_points, 0)
            for bbox_targets in bbox_targets_list
        ]
        mask_targets_list = [
            mask_targets.split(num_points, 0)
            for mask_targets in mask_targets_list
        ]

        # concat per level image
        concat_lvl_labels = []
        concat_lvl_centerness_targets = []
        concat_lvl_bbox_targets = []
        concat_lvl_mask_targets = []
        for i in range(num_levels):
            concat_lvl_labels.append(
                torch.cat([labels[i] for labels in labels_list]))
            concat_lvl_centerness_targets.append(
                torch.cat([centerness[i] for centerness in centerness_targets_list]))
            concat_lvl_bbox_targets.append(
                torch.cat(
                    [bbox_targets[i] for bbox_targets in bbox_targets_list]))
            concat_lvl_mask_targets.append(
                torch.cat(
                    [mask_targets[i] for mask_targets in mask_targets_list]))

        return concat_lvl_labels, concat_lvl_bbox_targets, concat_lvl_mask_targets, concat_lvl_centerness_targets

    def polar_centerness_target(self, pos_mask_targets):
        # only calculate pos centerness targets, otherwise there may be nan
        centerness_targets = (pos_mask_targets.min(dim=-1)[0] / pos_mask_targets.max(dim=-1)[0])
        return torch.sqrt(centerness_targets)

    @force_fp32(apply_to=('cls_scores', 'bbox_preds', 'centernesses'))
    def get_bboxes(self,
                   cls_scores,
                   bbox_preds,
                   centernesses,
                   mask_preds,
                   img_metas,
                   cfg,
                   rescale=None):
        assert len(cls_scores) == len(bbox_preds)
        num_levels = len(cls_scores)

        featmap_sizes = [featmap.size()[-2:] for featmap in cls_scores]
        mlvl_points = self.get_points(featmap_sizes, bbox_preds[0].dtype, bbox_preds[0].device)
        result_list = []
        for img_id in range(len(img_metas)):
            cls_score_list = [
                cls_scores[i][img_id].detach() for i in range(num_levels)
            ]
            bbox_pred_list = [
                bbox_preds[i][img_id].detach() for i in range(num_levels)
            ]
            centerness_pred_list = [
                centernesses[i][img_id].detach() for i in range(num_levels)
            ]
            mask_pred_list = [
                mask_preds[i][img_id].detach() for i in range(num_levels)
            ]
            img_shape = img_metas[img_id]['img_shape']
            scale_factor = img_metas[img_id]['scale_factor']
            det_bboxes = self.get_bboxes_single(cls_score_list,
                                                bbox_pred_list,
                                                mask_pred_list,
                                                centerness_pred_list,
                                                mlvl_points, img_shape,
                                                scale_factor, cfg, rescale)
            result_list.append(det_bboxes)
        return result_list

    def get_bboxes_single(self,
                          cls_scores,
                          bbox_preds,
                          mask_preds,
                          centernesses,
                          mlvl_points,
                          img_shape,
                          scale_factor,
                          cfg,
                          rescale=False):
        assert len(cls_scores) == len(bbox_preds) == len(mlvl_points)
        mlvl_bboxes = []
        mlvl_scores = []
        mlvl_masks = []
        mlvl_centerness = []
        for cls_score, bbox_pred, mask_pred, centerness, points in zip(
                cls_scores, bbox_preds, mask_preds, centernesses, mlvl_points):
            assert cls_score.size()[-2:] == bbox_pred.size()[-2:]
            scores = cls_score.permute(1, 2, 0).reshape(
                -1, self.cls_out_channels).sigmoid()

            centerness = centerness.permute(1, 2, 0).reshape(-1).sigmoid()
            bbox_pred = bbox_pred.permute(1, 2, 0).reshape(-1, 4)
            if self.use_fourier:
                mask_pred = mask_pred.permute(1, 2, 0).reshape(-1, self.num_coe * 2)
            else:
                mask_pred = mask_pred.permute(1, 2, 0).reshape(-1, self.contour_points)
            nms_pre = cfg.get('nms_pre', -1)
            if 0 < nms_pre < scores.shape[0]:
                max_scores, _ = (scores * centerness[:, None]).max(dim=1)
                _, topk_inds = max_scores.topk(nms_pre)
                points = points[topk_inds, :]
                bbox_pred = bbox_pred[topk_inds, :]
                mask_pred = mask_pred[topk_inds, :]
                scores = scores[topk_inds, :]
                centerness = centerness[topk_inds]
            if not self.bbox_from_mask:
                bboxes = distance2bbox(points, bbox_pred, max_shape=img_shape)
                # masks, _ = self.distance2mask(points, mask_pred, bbox=bboxes)
                masks, _ = self.distance2mask(points, mask_pred, max_shape=img_shape)
            else:
                masks, _ = self.distance2mask(points, mask_pred, max_shape=img_shape)
                bboxes = torch.stack([masks[:, 0].min(1)[0],
                                      masks[:, 1].min(1)[0],
                                      masks[:, 0].max(1)[0],
                                      masks[:, 1].max(1)[0]], -1)

            mlvl_bboxes.append(bboxes)
            mlvl_scores.append(scores)
            mlvl_centerness.append(centerness)
            mlvl_masks.append(masks)

        mlvl_bboxes = torch.cat(mlvl_bboxes)
        mlvl_masks = torch.cat(mlvl_masks)
        if rescale:
            _mlvl_bboxes = mlvl_bboxes / mlvl_bboxes.new_tensor(scale_factor)
            try:
                scale_factor = torch.Tensor(scale_factor)[:2].cuda().unsqueeze(1).repeat(1, self.contour_points)
                _mlvl_masks = mlvl_masks / scale_factor
            except:
                _mlvl_masks = mlvl_masks / mlvl_masks.new_tensor(scale_factor)

        mlvl_scores = torch.cat(mlvl_scores)
        padding = mlvl_scores.new_zeros(mlvl_scores.shape[0], 1)
        mlvl_scores = torch.cat([padding, mlvl_scores], dim=1)
        mlvl_centerness = torch.cat(mlvl_centerness)

        if self.mask_nms:
            '''1 mask->min_bbox->nms, performance same to origin box'''
            a = _mlvl_masks
            _mlvl_bboxes = torch.stack([a[:, 0].min(1)[0], a[:, 1].min(1)[0], a[:, 0].max(1)[0], a[:, 1].max(1)[0]], -1)
            det_bboxes, det_labels, det_masks = multiclass_nms_with_mask(
                _mlvl_bboxes,
                mlvl_scores,
                _mlvl_masks,
                cfg.score_thr,
                cfg.nms,
                cfg.max_per_img,
                score_factors=mlvl_centerness + self.centerness_factor)

        else:
            '''2 origin bbox->nms, performance same to mask->min_bbox'''
            det_bboxes, det_labels, det_masks = multiclass_nms_with_mask(
                _mlvl_bboxes,
                mlvl_scores,
                _mlvl_masks,
                cfg.score_thr,
                cfg.nms,
                cfg.max_per_img,
                score_factors=mlvl_centerness + self.centerness_factor)

        return det_bboxes, det_labels, det_masks

    # test
    def distance2mask(self, points, distances, max_shape=None, train=False, bbox=None):
        """Decode distance prediction to  mask points
        Args:
            points (Tensor): Shape (n, 2), [x, y].
            distances (Tensor): Distances from the given point to edge of contour.
            max_shape (tuple): Shape of the image.

        Returns:
            Tensor: Decoded masks.
        """
        if self.use_fourier:
            if train:
                distances = torch.irfft(torch.cat([distances, torch.zeros(distances.shape[0],
                                                                          self.contour_points - self.num_coe, 2).to(
                    "cuda")], 1), 1, True, False).float().exp()

            else:
                distances = distances.reshape(-1, self.num_coe, 2)
                distances = torch.irfft(torch.cat([distances[..., :self.visulize_coe, :],
                                                   torch.zeros(distances.shape[0],
                                                               self.contour_points - self.visulize_coe,
                                                               2).to("cuda")], 1), 1, True, False).float().exp()

        num_points = points.shape[0]
        points = points[:, :, None].repeat(1, 1, self.angles.shape[0])
        c_x, c_y = points[:, 0], points[:, 1]

        sin = torch.sin(self.angles)
        cos = torch.cos(self.angles)
        sin = sin[None, :].repeat(num_points, 1)
        cos = cos[None, :].repeat(num_points, 1)

        x = distances * sin + c_x
        y = distances * cos + c_y

        if max_shape is not None:
            x = x.clamp(min=0, max=max_shape[1] - 1)
            y = y.clamp(min=0, max=max_shape[0] - 1)
        if bbox is not None:
            x = torch.max(torch.min(x, bbox[:, 2].unsqueeze(1)), bbox[:, 0].unsqueeze(1))
            y = torch.max(torch.min(y, bbox[:, 3].unsqueeze(1)), bbox[:, 1].unsqueeze(1))

        res = torch.cat([x[:, None, :], y[:, None, :]], dim=1)
        return res, distances
