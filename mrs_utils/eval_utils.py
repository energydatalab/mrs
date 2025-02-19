"""

"""


# Built-in
import os
import re

# Libs
import scipy.special
import skimage.transform
import numpy as np
import toolman as tm
from tqdm import tqdm
from skimage import measure
from skimage.morphology import dilation, disk
import pydensecrf.densecrf as dcrf
from pydensecrf.utils import unary_from_softmax
from sklearn.metrics import precision_recall_curve, average_precision_score
from sklearn.metrics._ranking import _binary_clf_curve

# PyTorch
import torch
import torch.nn.functional as F

# Own modules
from data import patch_extractor, data_utils
from mrs_utils import vis_utils, metric_utils, misc_utils


def display_group(reg_groups, size, img=None, need_return=False):
    """
    Visualize grouped connected components
    :param reg_groups: grouped connected components, can get this by calling ObjectScorer._group_pairs
    :param size: the size of the image or gt
    :param img: if given, the image will be displayed together with the visualization
    :param need_return: if True, the rendered image will be returned, otherwise the image will be displayed
    :return:
    """
    group_map = np.zeros(size, dtype=np.int)
    for cnt, g in enumerate(reg_groups):
        # for g in group:
        coords = np.array(g.coords)
        group_map[coords[:, 0], coords[:, 1]] = cnt + 1
    if need_return:
        return group_map
    else:
        if img:
            vis_utils.compare_figures([img, group_map], (1, 2), fig_size=(12, 5))
        else:
            vis_utils.compare_figures([group_map], (1, 1), fig_size=(8, 6))


def get_stats_from_group(reg_group, conf_img=None):
    """
    Get the coordinates of all pixels within the group and also mean of confidence
    :param reg_group:
    :param conf_img:
    :return:
    """
    coords = []
    # for g in reg_group:
    coords.extend(reg_group.coords)
    coords = np.array(coords)
    if conf_img is not None:
        conf = np.mean(conf_img[coords[:, 0], coords[:, 1]])
        return coords, conf
    else:
        return coords


def coord_iou(coords_a, coords_b):
    """
    This code comes from https://stackoverflow.com/a/42874377
    :param coords_a:
    :param coords_b:
    :return:
    """
    y1, x1 = np.min(coords_a, axis=0)
    y2, x2 = np.max(coords_a, axis=0)
    bb1 = {'x1': x1, 'y1': y1, 'x2': x2, 'y2': y2}
    y1, x1 = np.min(coords_b, axis=0)
    y2, x2 = np.max(coords_b, axis=0)
    bb2 = {'x1': x1, 'y1': y1, 'x2': x2, 'y2': y2}

    assert bb1['x1'] <= bb1['x2']
    assert bb1['y1'] <= bb1['y2']
    assert bb2['x1'] <= bb2['x2']
    assert bb2['y1'] <= bb2['y2']

    x_left = max(bb1['x1'], bb2['x1'])
    y_top = max(bb1['y1'], bb2['y1'])
    x_right = min(bb1['x2'], bb2['x2'])
    y_bottom = min(bb1['y2'], bb2['y2'])

    if x_right < x_left or y_bottom < y_top:
        return 0.0
    intersection_area = (x_right - x_left) * (y_bottom - y_top)
    bb1_area = (bb1['x2'] - bb1['x1']) * (bb1['y2'] - bb1['y1'])
    bb2_area = (bb2['x2'] - bb2['x1']) * (bb2['y2'] - bb2['y1'])
    iou = intersection_area / float(bb1_area + bb2_area - intersection_area)
    iou = np.minimum(np.maximum(iou, 0), 1)
    return iou


def compute_iou(coords_a, coords_b, size):
    """
    Compute object-wise IoU
    :param self:
    :param coords_a:
    :param coords_b:
    :param size:
    :return:
    """
    # compute bbox IoU since this is faster
    iou = coord_iou(coords_a, coords_b)
    if iou > 0:
        # if bboxes overlaps, compute object-wise IoU
        tile_a = np.zeros(size)
        tile_a[coords_a[:, 0], coords_a[:, 1]] = 1
        tile_b = np.zeros(size)
        tile_b[coords_b[:, 0], coords_b[:, 1]] = 1
        return metric_utils.iou_metric(tile_a, tile_b, divide=True)
    else:
        return 0


class ObjectScorer(object):
    def __init__(self, min_region=5, min_th=0.5, dilation_size=12):
        """
        Object-wise scoring metric: the conf map instead of prediction map is needed
        The conf map will first be binarized by certain threshold, then any connected components
        smaller than certain region will be discarded
        Any connected components within certain range are further grouped
        For getting precision and recall, first compute grouped object-wise IoU. An object in pred
        will be "linked" to a gt when IoU is greater than a threshold. We then define:
        TP: A prediction is linked to gt
        FP: A prediction has no gt to be linked
        FN: A gt has no prediction to be linked
        :param min_region: the smallest #pixels to form an object
        :param min_th: the threshold to binarize the conf map
        """
        self.min_region = min_region
        self.min_th = min_th
        self.dilation_size = dilation_size

    @staticmethod
    def _reg_to_centroids(reg_props):
        """
        Get the centroids of given region proposals
        :param reg_props: the region proposal generated by skimage.measure
        :return:
        """
        return [[int(c) for c in rp.centroid] for rp in reg_props]

    @staticmethod
    def _group_pairs(cps, reg_props):
        """
        Group connected components together
        :param cps:
        :param reg_props:
        :return:
        """
        groups = []
        obj_ids = list(range(len(reg_props)))
        for cp in cps:
            flag = True
            for group in groups:
                if cp[0] in group or cp[1] in group:
                    group.update(cp)
                    flag = False
                    break
            if flag:
                groups.append(set(cp))
                for c in cp:
                    obj_ids.remove(c)
        for obj_id in obj_ids:
            groups.append({obj_id})

        reg_groups = []
        for group in groups:
            reg_groups.append([reg_props[g] for g in group])
        return reg_groups

    def get_object_groups(self, conf_map):
        """
        Group objects within certain radius
        :param conf_map:
        :return:
        """
        # get connected components
        im_binary = conf_map >= self.min_th

        # do min_region thresholding before adding dilation
        im_label = measure.label(im_binary)
        reg_props = [a for a in measure.regionprops(im_label, conf_map) if a.area >= self.min_region]
        
        # rasterize post min_region map
        dummy = np.zeros(im_label.shape, dtype=int)
        for reg in reg_props:
            coords = np.array(reg.coords)
            dummy[coords[:, 0], coords[:, 1]] = 1
        im_binary = dummy
        
        # add dilation
        if self.dilation_size < 0 or not isinstance(self.dilation_size, int):
            return ValueError("Dilation size must be a positive integer")
        elif self.dilation_size > 0:
            im_dilated = dilation(im_binary, disk(self.dilation_size))

        dilated_label = measure.label(im_dilated)
        pixel_grouped = np.multiply(im_binary, dilated_label)
        pixel_grouped_reg_props = measure.regionprops(pixel_grouped, conf_map)

        # Remove regions whose un-dilated area are lower than threshold
        pixel_grouped_reg_props = [a for a in pixel_grouped_reg_props if a.area >= self.min_region]

        return pixel_grouped_reg_props


def score(pred, lbl, min_region=5, min_th=0.5, dilation_size=5, iou_th=0.5):
    obj_scorer = ObjectScorer(min_region, min_th, dilation_size)

    group_pred = obj_scorer.get_object_groups(pred)
    group_lbl = obj_scorer.get_object_groups(lbl)

    conf_list, true_list = [], []
    linked_pred = []

    for _, g_lbl in enumerate(group_lbl):
        link_flag = False
        for cnt, g_pred in enumerate(group_pred):
            coords_pred, conf = get_stats_from_group(g_pred, pred)
            coords_lbl = get_stats_from_group(g_lbl)
            iou = compute_iou(coords_pred, coords_lbl, pred.shape)
            if iou >= iou_th and cnt not in linked_pred:
                # TP
                conf_list.append(conf)
                true_list.append(1)
                linked_pred.append(cnt)
                link_flag = True
                break
        if not link_flag:
            # FN
            conf_list.append(-1)
            true_list.append(1)

    for cnt, g_pred in enumerate(group_pred):
        if cnt not in linked_pred:
            # FP
            _, conf = get_stats_from_group(g_pred, pred)
            conf_list.append(conf)
            true_list.append(0)
    return conf_list, true_list


def batch_score(pred_files, lbl_files, min_region=5, min_th=0.5, iou_th=0.5):
    conf, true = [], []
    for pred_file, lbl_file in tqdm(zip(pred_files, lbl_files), total=len(pred_files)):
        pred, lbl = misc_utils.load_file(pred_file), misc_utils.load_file(lbl_file)
        conf_, true_ = score(pred, lbl, min_region, min_th, iou_th)
        conf.extend(conf_)
        true.extend(true_)
    return conf, true


def read_results(result_name, regex=None, sum_results=False, delta=1e-6, class_names=None):
    """
    Read and parse evaluated results text file
    :param result_name: path to the results file
    :param regex: if given, it will be applied to select lines that match the name
    :param sum_results: if True, return the IoU of the overall dataset
    :param delta: a small value to prevent divided by zero
    :param class_names: list of strings for class names, if None, they will be class_i
    :return:
    """
    def update_results(res, n, i_res, c_names):
        if c_names is not None:
            assert len(i_res) == 2 * len(c_names)
        else:
            c_names = ['class_{}'.format(i) for i in range(len(i_res) // 2)]
        for cnt, c_name in enumerate(c_names):
            res[n][c_name+'_a'] = i_res[cnt * 2]
            res[n][c_name+'_b'] = i_res[cnt * 2 + 1]
        return c_names

    def combine_results(res, i_res):
        if res is None:
            res = dict()
            for k, v in i_res.items():
                if k != 'iou':
                    res.update({k: v})
        else:
            for k, v in i_res.items():
                if k != 'iou':
                    if k in res:
                        res[k] += v
                    else:
                        res.update({k: v})
        return res

    def summarize_results(res):
        sum_res = dict()
        for c_name in class_names+['iou']:
            res[c_name] = (float(res[c_name + '_a']) / float(res[c_name + '_b']) + delta) * 100
        if 'iou' in res:
            sum_res['iou'] = res['iou']
        else:
            overall_iou = []
            for c_name in class_names:
                overall_iou.append(sum_res[c_name])
            sum_res['iou'] = np.mean(overall_iou)
        return sum_res

    results = {}
    result_lines = misc_utils.load_file(result_name)

    for line in result_lines:
        if len(line) <= 1:
            continue
        name, iou_a, iou_b, *ious, iou = line.strip().split(',')
        iou_a, iou_b, iou = float(iou_a), float(iou_b), float(iou)
        results[name] = {'iou': iou, 'iou_a': iou_a, 'iou_b': iou_b}
        class_names = update_results(results, name, ious, class_names)
    if regex:
        comb_res = None
        for key, val in results.items():
            if re.search(regex, key):
                comb_res = combine_results(comb_res, val)
        return summarize_results(comb_res)
    elif sum_results:
        return summarize_results(results['Overall'])
    else:
        return results


def get_precision_recall(conf, true):
    # ap = average_precision_score(true, conf)
    p, r, th = precision_recall_curve(true, conf)
    p[0] = 0
    r[0] = r[1] + 0.01

    x = np.linspace(0, 1, 101)  # 101-point interp (COCO)
    ap = np.trapz(np.interp(x, r[::-1], p[::-1]), x)  # calculate ap with trapezoidal rule
    return ap, p, r, th


def get_fps(conf, true):
    
    fps, tps, th = _binary_clf_curve(true, conf)
    last_ind = tps.searchsorted(tps[-1])
    sl = slice(last_ind, None, -1)
    
    return fps[sl]


class Evaluator:
    def __init__(self, ds_name, data_dir, tsfm, device, load_func=None, infer=False, ensembler=None, **kwargs):
        ds_name = misc_utils.stem_string(ds_name)
        self.tsfm = tsfm
        self.device = device
        if ensembler is None:
            self.ensembler = BaseEnsemble()
        else:
            self.ensembler = ensembler
        if ds_name == 'inria':
            from data.inria import preprocess
            self.rgb_files, self.lbl_files = preprocess.get_images(data_dir, **kwargs)
            assert len(self.rgb_files) == len(self.lbl_files)
            self.truth_val = 255
            self.decode_func = None
            self.encode_func = None
            self.class_names = ['building', ]
        elif ds_name == 'deepglobe':
            from data.deepglobe import preprocess
            self.rgb_files, self.lbl_files = preprocess.get_images(data_dir)
            assert len(self.rgb_files) == len(self.lbl_files)
            self.truth_val = 1
            self.decode_func = None
            self.encode_func = lambda x: x * 255
            self.class_names = ['building', ]
        elif ds_name == 'deepgloberoad':
            from data.deepgloberoad import preprocess
            self.rgb_files, self.lbl_files = preprocess.get_images(data_dir, **kwargs)
            assert len(self.rgb_files) == len(self.lbl_files)
            self.truth_val = 255
            self.decode_func = preprocess.decode_map
            self.encode_func = None
            self.class_names = ['road', ]
        elif ds_name == 'deepglobeland':
            from data.deepglobeland import preprocess
            if not infer:
                self.rgb_files, self.lbl_files = preprocess.get_images(data_dir, **kwargs)
            else:
                self.rgb_files, self.lbl_files = preprocess.get_test_images(data_dir, **kwargs)
            assert len(self.rgb_files) == len(self.lbl_files)
            self.truth_val = 1
            self.decode_func = preprocess.decode_map
            self.encode_func = preprocess.encode_map
            self.class_names = preprocess.CLASS_NAMES[:6]
        elif ds_name == 'mnih':
            from data.mnih import preprocess
            self.rgb_files, self.lbl_files = preprocess.get_images(data_dir, **kwargs)
            assert len(self.rgb_files) == len(self.lbl_files)
            self.truth_val = 255
            self.decode_func = None
            self.encode_func = None
            self.class_names = ['road', ]
        elif ds_name == 'spca':
            from data.spca import preprocess
            self.rgb_files, self.lbl_files = preprocess.get_images(data_dir, **kwargs)
            assert len(self.rgb_files) == len(self.lbl_files)
            self.truth_val = 1
            self.decode_func = None
            self.encode_func = None
            self.class_names = ['panel',]
        elif load_func:
            self.truth_val = kwargs.pop('truth_val', 1)
            self.decode_func = kwargs.pop('decode_func', None)
            self.encode_func = kwargs.pop('encode_func', None)
            self.class_names = kwargs.pop('class_names', ['building', ])
            self.rgb_files, self.lbl_files = load_func(data_dir, **kwargs)
            assert len(self.rgb_files) == len(self.lbl_files)
        else:
            raise NotImplementedError('Dataset {} is not supported')

    def get_result_strings(self, file_name, iou_score, delta=1e-6):
        print_string = '{}: IoU={:05.2f}\n\t'.format(file_name, np.mean(iou_score[0, :] / (iou_score[1, :] + delta) * 100))
        for c_cnt, class_name in enumerate(self.class_names):
            print_string += ' {}: IoU={:05.2f}'.format(class_name, iou_score[0, c_cnt] / (iou_score[1, c_cnt] + delta) * 100)
        report_string = '{},{},{}'.format(file_name, np.sum(iou_score[0, :]), np.sum(iou_score[1, :]))
        if len(self.class_names) > 1:
            for c_cnt, class_name in enumerate(self.class_names):
                report_string += ',{},{}'.format(iou_score[0, c_cnt], iou_score[1, c_cnt])
        report_string += ',{}\n'.format(np.mean(iou_score[0, :] / (iou_score[1, :] + delta) * 100))
        return print_string, report_string

    def evaluate(self, model, patch_size, overlap, pred_dir=None, report_dir=None, save_conf=False, delta=1e-6,
                 eval_class=(1, ), visualize=False, densecrf=False, crf_params=None, verbose=True):
        if isinstance(model, list) or isinstance(model, tuple):
            lbl_margin = model[0].lbl_margin
        else:
            lbl_margin = model.lbl_margin
        if crf_params is None and densecrf:
            crf_params = {'sxy': 3, 'srgb': 3, 'compat': 5}

        iou_a, iou_b = np.zeros(len(eval_class)), np.zeros(len(eval_class))
        report = []
        if pred_dir:
            misc_utils.make_dir_if_not_exist(pred_dir)
        for rgb_file, lbl_file in zip(self.rgb_files, self.lbl_files):
            file_name = os.path.splitext(os.path.basename(lbl_file))[0]

            # read data
            rgb = misc_utils.load_file(rgb_file)[:, :, :3]
            lbl = misc_utils.load_file(lbl_file)
            if self.decode_func:
                lbl = self.decode_func(lbl)

            # evaluate on tiles
            tile_dim = rgb.shape[:2]
            tile_dim_pad = [tile_dim[0]+2*lbl_margin, tile_dim[1]+2*lbl_margin]
            grid_list = patch_extractor.make_grid(tile_dim_pad, patch_size, overlap)

            if isinstance(model, list) or isinstance(model, tuple):
                tile_preds = 0
                for m in model:
                    tile_preds = tile_preds + self.infer_tile(m, rgb, grid_list, patch_size, tile_dim, tile_dim_pad, lbl_margin)
            else:
                tile_preds = self.infer_tile(model, rgb, grid_list, patch_size, tile_dim, tile_dim_pad, lbl_margin)

            if save_conf:
                misc_utils.save_file(os.path.join(pred_dir, '{}.npy'.format(file_name)), tile_preds[:, :, 1])

            if densecrf:
                d = dcrf.DenseCRF2D(*tile_preds.shape)
                U = unary_from_softmax(np.ascontiguousarray(
                    data_utils.change_channel_order(tile_preds, False)))
                d.setUnaryEnergy(U)
                d.addPairwiseBilateral(rgbim=rgb, **crf_params)
                Q = d.inference(5)
                tile_preds = np.argmax(Q, axis=0).reshape(*tile_preds.shape[:2])
            else:
                tile_preds = np.argmax(tile_preds, -1)
            iou_score = metric_utils.iou_metric(lbl/self.truth_val, tile_preds, eval_class=eval_class)
            pstr, rstr = self.get_result_strings(file_name, iou_score, delta)
            tm.misc_utils.verb_print(pstr, verbose)
            report.append(rstr)
            iou_a += iou_score[0, :]
            iou_b += iou_score[1, :]
            if visualize:
                if self.encode_func:
                    vis_utils.compare_figures([rgb, self.encode_func(lbl), self.encode_func(tile_preds)], (1, 3),
                                              fig_size=(15, 5))
                else:
                    vis_utils.compare_figures([rgb, lbl, tile_preds], (1, 3), fig_size=(15, 5))
            if pred_dir:
                if self.encode_func:
                    misc_utils.save_file(os.path.join(pred_dir, '{}.png'.format(file_name)), self.encode_func(tile_preds))
                else:
                    misc_utils.save_file(os.path.join(pred_dir, '{}.png'.format(file_name)), tile_preds)
        pstr, rstr = self.get_result_strings('Overall', np.stack([iou_a, iou_b], axis=0), delta)
        tm.misc_utils.verb_print(pstr, verbose)
        report.append(rstr)
        if report_dir:
            misc_utils.make_dir_if_not_exist(report_dir)
            misc_utils.save_file(os.path.join(report_dir, 'result.txt'), report)
        return np.mean(iou_a / (iou_b + delta))*100

    def infer_tile(self, model, rgb, grid_list, patch_size, tile_dim, tile_dim_pad, lbl_margin):
        tile_preds = []
        for patch in patch_extractor.patch_block(rgb, model.lbl_margin, grid_list, patch_size, False):
            patch_preds = []
            for aug_patch in self.ensembler.augment_data(patch):
                for tsfm in self.tsfm:
                    tsfm_image = tsfm(image=aug_patch)
                    aug_patch = tsfm_image['image']
                aug_patch = torch.unsqueeze(aug_patch, 0).to(self.device)
                pred = F.softmax(model.inference(aug_patch), 1).detach().cpu().numpy()
                patch_preds.append(pred)
            tile_preds.append(data_utils.change_channel_order(self.ensembler.fuse_data(patch_preds), True)[0, :, :, :])
        # stitch back to tiles
        tile_preds = patch_extractor.unpatch_block(
            np.array(tile_preds),
            tile_dim_pad,
            patch_size,
            tile_dim,
            [patch_size[0] - 2 * lbl_margin, patch_size[1] - 2 * lbl_margin],
            overlap=2 * lbl_margin
        )
        return tile_preds

    def infer(self, model, pred_dir, patch_size, overlap, ext='_mask', file_ext='png', visualize=False,
              densecrf=False, crf_params=None, save_conf=False):
        if isinstance(model, list) or isinstance(model, tuple):
            lbl_margin = model[0].lbl_margin
        else:
            lbl_margin = model.lbl_margin
        if crf_params is None and densecrf:
            crf_params = {'sxy': 3, 'srgb': 3, 'compat': 5}

        misc_utils.make_dir_if_not_exist(pred_dir)
        pbar = tqdm(self.rgb_files)
        for rgb_file in pbar:
            file_name = os.path.splitext(os.path.basename(rgb_file))[0].split('.')[0]
            pbar.set_description('Inferring {}'.format(file_name))
            # read data
            rgb = misc_utils.load_file(rgb_file)[:, :, :3]

            # evaluate on tiles
            tile_dim = rgb.shape[:2]
            tile_dim_pad = [tile_dim[0] + 2 * lbl_margin, tile_dim[1] + 2 * lbl_margin]
            grid_list = patch_extractor.make_grid(tile_dim_pad, patch_size, overlap)

            if isinstance(model, list) or isinstance(model, tuple):
                tile_preds = 0
                for m in model:
                    tile_preds = tile_preds + self.infer_tile(m, rgb, grid_list, patch_size, tile_dim, tile_dim_pad,
                                                              lbl_margin)
            else:
                tile_preds = self.infer_tile(model, rgb, grid_list, patch_size, tile_dim, tile_dim_pad, lbl_margin)

            if save_conf:
                misc_utils.save_file(os.path.join(pred_dir, '{}_conf.png'.format(file_name)), (tile_preds[:, :, 1] * 255).astype(np.uint8))

            if densecrf:
                d = dcrf.DenseCRF2D(*tile_preds.shape)
                U = unary_from_softmax(np.ascontiguousarray(
                    data_utils.change_channel_order(tile_preds, False)))
                d.setUnaryEnergy(U)
                d.addPairwiseBilateral(rgbim=rgb, **crf_params)
                Q = d.inference(5)
                tile_preds = np.argmax(Q, axis=0).reshape(*tile_preds.shape[:2])
            else:
                tile_preds = np.argmax(tile_preds, -1)

            if self.encode_func:
                pred_img = self.encode_func(tile_preds)
            else:
                pred_img = tile_preds

            if visualize:
                vis_utils.compare_figures([rgb, pred_img], (1, 2), fig_size=(12, 5))


class BaseEnsemble(object):
    @staticmethod
    def augment_data(img):
        return [img, ]

    @staticmethod
    def fuse_data(img):
        return img[0]


class MultiResEnsemble(BaseEnsemble):
    def __init__(self, aug_size, fuse_size=None, rotate=True, use_max=False):
        self.aug_size = aug_size
        self.rotate = rotate
        if self.rotate:
            self.copy_per_img = 6
        else:
            self.copy_per_img = 1
        if not fuse_size:
            fuse_size = self.aug_size[-1]
        self.fuse_size = fuse_size
        self.use_max = use_max

    def augment_data(self, img):
        aug_images = []
        for aug_size in self.aug_size:
            rgb = skimage.transform.resize(img, (aug_size, aug_size), preserve_range=True).astype(np.uint8)
            aug_images.append(rgb)
            if self.rotate:
                aug_images.append(rgb[::-1, :, :])
                aug_images.append(rgb[:, ::-1, :])
                aug_images.append(np.rot90(rgb))
                aug_images.append(np.rot90(rgb)[::-1, :, :])
                aug_images.append(np.rot90(rgb)[:, ::-1, :])
        return aug_images

    def fuse_data(self, imgs):
        fuse_images = [[] for _ in range(len(self.aug_size))]
        for cnt, img in enumerate(imgs):
            rgb = skimage.transform.resize(data_utils.change_channel_order(img[0, :, :, :], to_channel_last=True),
                                           (self.fuse_size, self.fuse_size))
            if cnt % self.copy_per_img == 0:
                fuse_images[cnt // self.copy_per_img].append(rgb)
            elif cnt % self.copy_per_img == 1:
                fuse_images[cnt // self.copy_per_img].append(rgb[::-1, :, :])
            elif cnt % self.copy_per_img == 2:
                fuse_images[cnt // self.copy_per_img].append(rgb[:, ::-1, :])
            elif cnt % self.copy_per_img == 3:
                fuse_images[cnt // self.copy_per_img].append(np.rot90(rgb, k=-1))
            elif cnt % self.copy_per_img == 4:
                fuse_images[cnt // self.copy_per_img].append(np.rot90(rgb[::-1, :, :], k=-1))
            elif cnt % self.copy_per_img == 5:
                fuse_images[cnt // self.copy_per_img].append(np.rot90(rgb[:, ::-1, :], k=-1))
        fuse_tps = [np.mean(np.stack(a, axis=0), axis=0) for a in fuse_images]

        if self.use_max:
            pred = np.max(np.stack(fuse_tps, axis=0), axis=0)
        else:
            pred = np.mean(np.stack(fuse_tps, axis=0), axis=0)
        return np.expand_dims(data_utils.change_channel_order(pred, to_channel_last=False), axis=0)


if __name__ == '__main__':
    rgb_file = r'/media/ei-edl01/data/remote_sensing_data/inria/images/austin1.tif'
    lbl_file = r'/media/ei-edl01/data/remote_sensing_data/inria/gt/austin1.tif'
    conf_file = r'/hdd/Results/mrs/inria/ecresnet50_dcunet_dsinria_lre1e-04_lrd1e-04_ep50_bs7_ds50_dr0p1/austin1.npy'
    rgb = misc_utils.load_file(rgb_file)
    lbl_img, conf_img = misc_utils.load_file(lbl_file) / 255, misc_utils.load_file(conf_file)

    osc = ObjectScorer(min_region=5, min_th=0.5)
    lbl_groups = osc.get_object_groups(lbl_img)
    conf_groups = osc.get_object_groups(conf_img)
    print(len(lbl_groups), len(conf_groups))
    lbl_group_img = display_group(lbl_groups, lbl_img.shape[:2], need_return=True)
    conf_group_img = display_group(conf_groups, conf_img.shape[:2], need_return=True)
    vis_utils.compare_figures([rgb, lbl_group_img, conf_group_img], (1, 3), fig_size=(15, 5))
