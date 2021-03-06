#!/usr/bin/env python

import numpy as np
import pandas as pd
import logging
import subprocess
import os, sys
import torch
from collections import OrderedDict
import plotting
import importlib.util
from copy import deepcopy
import SimpleITK as sitk
from preprocessing.preprocessor import get_lowres_axis, get_do_separate_z, resample_data_or_seg
from utils.files_utils import *

def get_logger(exp_dir):
    """
    creates logger instance. writing out info to file and to terminal.
    :param exp_dir: experiment directory, where exec.log file is stored.
    :return: logger instance.
    """

    logger = logging.getLogger('auto_med_detection')
    logger.setLevel(logging.DEBUG)
    log_file = exp_dir + '/exec.log'
    hdlr = logging.FileHandler(log_file)
    print('Logging to {}'.format(log_file))
    logger.addHandler(hdlr)
    logger.addHandler(ColorHandler())
    logger.propagate = False
    return logger



def prep_exp(dataset_path, exp_path, server_env, use_stored_settings=True, is_training=True):

    if is_training:

        # the first process of an experiment creates the directories and copies the config to exp_path.
        if not os.path.exists(exp_path):
            os.mkdir(exp_path)
            os.mkdir(os.path.join(exp_path, 'plots'))
            subprocess.call('cp {} {}'.format(os.path.join(dataset_path, 'configs.py'), os.path.join(exp_path, 'configs.py')), shell=True)
            subprocess.call('cp {} {}'.format('default_configs.py', os.path.join(exp_path, 'default_configs.py')), shell=True)


        if use_stored_settings:
            subprocess.call('cp {} {}'.format('default_configs.py', os.path.join(exp_path, 'default_configs.py')), shell=True)
            cf_file = import_module('cf', os.path.join(exp_path, 'configs.py'))
            cf = cf_file.configs(server_env)
            # only the first process copies the model selcted in configs to exp_path.
            if not os.path.isfile(os.path.join(exp_path, 'model.py')):
                subprocess.call('cp {} {}'.format(cf.model_path, os.path.join(exp_path, 'model.py')), shell=True)
                subprocess.call('cp {} {}'.format(os.path.join(cf.backbone_path), os.path.join(exp_path, 'backbone.py')), shell=True)

            # copy the snapshot model scripts from exp_dir back to the source_dir as tmp_model / tmp_backbone.
            tmp_model_path = os.path.join(cf.source_dir, 'models', 'tmp_model.py')
            tmp_backbone_path = os.path.join(cf.source_dir, 'models', 'tmp_backbone.py')
            subprocess.call('cp {} {}'.format(os.path.join(exp_path, 'model.py'), tmp_model_path), shell=True)
            subprocess.call('cp {} {}'.format(os.path.join(exp_path, 'backbone.py'), tmp_backbone_path), shell=True)
            cf.model_path = tmp_model_path
            cf.backbone_path = tmp_backbone_path

        else:
            # run training with source code info and copy snapshot of model to exp_dir for later testing (overwrite scripts if exp_dir already exists.)
            cf_file = import_module('cf', os.path.join(dataset_path, 'configs.py'))
            cf = cf_file.configs(server_env)
            subprocess.call('cp {} {}'.format(cf.model_path, os.path.join(exp_path, 'model.py')), shell=True)
            subprocess.call('cp {} {}'.format(cf.backbone_path, os.path.join(exp_path, 'backbone.py')), shell=True)
            subprocess.call('cp {} {}'.format('default_configs.py', os.path.join(exp_path, 'default_configs.py')), shell=True)
            subprocess.call('cp {} {}'.format(os.path.join(dataset_path, 'configs.py'), os.path.join(exp_path, 'configs.py')), shell=True)

    else:
        # for testing copy the snapshot model scripts from exp_dir back to the source_dir as tmp_model / tmp_backbone.
        cf_file = import_module('cf', os.path.join(exp_path, 'configs.py'))
        cf = cf_file.configs(server_env)
        if cf.hold_out_test_set:
            cf.pp_data_path = cf.pp_test_data_path
            cf.pp_name = cf.pp_test_name
        tmp_model_path = os.path.join(cf.source_dir, 'models', 'tmp_model.py')
        tmp_backbone_path = os.path.join(cf.source_dir, 'models', 'tmp_backbone.py')
        subprocess.call('cp {} {}'.format(os.path.join(exp_path, 'model.py'), tmp_model_path), shell=True)
        subprocess.call('cp {} {}'.format(os.path.join(exp_path, 'backbone.py'), tmp_backbone_path), shell=True)
        cf.model_path = tmp_model_path
        cf.backbone_path = tmp_backbone_path

    cf.exp_dir = exp_path
    cf.test_dir = os.path.join(cf.exp_dir, 'test')
    cf.plot_dir = os.path.join(cf.exp_dir, 'plots')
    cf.experiment_name = exp_path.split("/")[-1]
    cf.server_env = server_env
    cf.created_fold_id_pickle = False

    return cf



def store_seg_from_softmax(segmentation_softmax, out_fname, dct, order=1, region_class_order=None,
                                         seg_postprogess_fn=None, seg_postprocess_args=None, resampled_npz_fname=None,
                                         non_postprocessed_fname=None):

    if isinstance(segmentation_softmax, str):
        assert isfile(segmentation_softmax), "If isinstance(segmentation_softmax, str) then " \
                                             "isfile(segmentation_softmax) must be True"
        del_file = deepcopy(segmentation_softmax)
        segmentation_softmax = np.load(segmentation_softmax)
        os.remove(del_file)

    # first resample, then put result into bbox of cropping, then save
    current_shape = segmentation_softmax.shape
    shape_original_after_cropping = dct.get('size_after_cropping')
    shape_original_before_cropping = dct.get('original_size_of_raw_data')

    if np.any(np.array(current_shape) != np.array(shape_original_after_cropping)):
        if get_do_separate_z(dct.get('original_spacing')):
            do_separate_z = True
            lowres_axis = get_lowres_axis(dct.get('original_spacing'))
        elif get_do_separate_z(dct.get('spacing_after_resampling')):
            do_separate_z = True
            lowres_axis = get_lowres_axis(dct.get('spacing_after_resampling'))
        else:
            do_separate_z = False
            lowres_axis = None

        print("separate z:",do_separate_z, "lowres axis", lowres_axis)
        seg_old_spacing = resample_data_or_seg(segmentation_softmax, shape_original_after_cropping, is_seg=False,
                                               axis=lowres_axis, order=order, do_separate_z=do_separate_z, cval=0)
        #seg_old_spacing = resize_softmax_output(segmentation_softmax, shape_original_after_cropping, order=order)
    else:
        seg_old_spacing = segmentation_softmax

    if resampled_npz_fname is not None:
        np.savez_compressed(resampled_npz_fname, softmax=seg_old_spacing.astype(np.float16))
        save_pickle(dct, resampled_npz_fname[:-4] + ".pkl")

    if region_class_order is None:
        seg_old_spacing = seg_old_spacing.argmax(0)
    else:
        seg_old_spacing_final = np.zeros(seg_old_spacing.shape[1:])
        for i, c in enumerate(region_class_order):
            seg_old_spacing_final[seg_old_spacing[i] > 0.5] = c
        seg_old_spacing = seg_old_spacing_final

    bbox = dct.get('crop_bbox')

    if bbox is not None:
        seg_old_size = np.zeros(shape_original_before_cropping)
        for c in range(3):
            bbox[c][1] = np.min((bbox[c][0] + seg_old_spacing.shape[c], shape_original_before_cropping[c]))
        seg_old_size[bbox[0][0]:bbox[0][1],
        bbox[1][0]:bbox[1][1],
        bbox[2][0]:bbox[2][1]] = seg_old_spacing
    else:
        seg_old_size = seg_old_spacing

    if seg_postprogess_fn is not None:
        seg_old_size_postprocessed = seg_postprogess_fn(np.copy(seg_old_size), *seg_postprocess_args)
    else:
        seg_old_size_postprocessed = seg_old_size

    seg_resized_itk = sitk.GetImageFromArray(seg_old_size_postprocessed.astype(np.uint8))
    seg_resized_itk.SetSpacing(dct['itk_spacing'])
    seg_resized_itk.SetOrigin(dct['itk_origin'])
    seg_resized_itk.SetDirection(dct['itk_direction'])
    sitk.WriteImage(seg_resized_itk, out_fname)

    if (non_postprocessed_fname is not None) and (seg_postprogess_fn is not None):
        seg_resized_itk = sitk.GetImageFromArray(seg_old_size.astype(np.uint8))
        seg_resized_itk.SetSpacing(dct['itk_spacing'])
        seg_resized_itk.SetOrigin(dct['itk_origin'])
        seg_resized_itk.SetDirection(dct['itk_direction'])
        sitk.WriteImage(seg_resized_itk, non_postprocessed_fname)



def import_module(name, path):
    """
    correct way of importing a module dynamically in python 3.
    :param name: name given to module instance.
    :param path: path to module.
    :return: module: returned module instance.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module



class ModelSelector:
    '''
    saves a checkpoint after each epoch as 'last_state'.
    saves the top-k (k=cf.save_n_models) ranked epochs. 
    In inference, predictions of multiple epochs can be ensembled to improve performance.
    '''

    def __init__(self, cf, logger):

        self.cf = cf
        self.saved_epochs = [-1] * cf.save_n_models
        self.logger = logger

    def do_model_selection(self, net, optimizer, monitor_metrics, epoch):

        # take the mean over all selection criteria in each epoch
        non_nan_scores = np.mean(np.array([[0 if ii is None else ii for ii in monitor_metrics['val'][sc]] for sc in self.cf.model_selection_criteria]), 0)
        print('non none scores:', non_nan_scores)
        epochs_scores = [ii for ii in non_nan_scores[1:]]
        # ranking of epochs according to model_selection_criterion
        epoch_ranking = np.argsort(epochs_scores)[::-1] + 1 #epochs start at 1
        # if set in configs, epochs < min_save_thresh are discarded from saving process.
        epoch_ranking = epoch_ranking[epoch_ranking >= self.cf.min_save_thresh]

        # check if current epoch is among the top-k epchs.
        if epoch in epoch_ranking[:self.cf.save_n_models]:
            torch.save(net.state_dict(), os.path.join(self.cf.fold_dir, '{}_best_params.pth'.format(epoch)))
            # save epoch_ranking to keep info for inference.
            np.save(os.path.join(self.cf.fold_dir, 'epoch_ranking'), epoch_ranking[:self.cf.save_n_models])
            self.logger.info(
                "saving current epoch {} at rank {}".format(epoch, np.argwhere(epoch_ranking == epoch)))
            # delete params of the epoch that just fell out of the top-k epochs.
            for se in [int(ii.split('_')[0]) for ii in os.listdir(self.cf.fold_dir) if 'best_params' in ii]:
                if se in epoch_ranking[self.cf.save_n_models:]:
                    subprocess.call('rm {}'.format(os.path.join(self.cf.fold_dir, '{}_best_params.pth'.format(se))), shell=True)
                    self.logger.info('deleting epoch {} at rank {}'.format(se, np.argwhere(epoch_ranking == se)))

        state = {
            'epoch': epoch,
            'state_dict': net.state_dict(),
            'optimizer': optimizer.state_dict(),
        }

        torch.save(state, os.path.join(self.cf.fold_dir, 'last_state.pth'))



def load_checkpoint(checkpoint_path, net, optimizer):

    checkpoint = torch.load(checkpoint_path)
    net.load_state_dict(checkpoint['state_dict'])
    optimizer.load_state_dict(checkpoint['optimizer'])
    return checkpoint['epoch']



def prepare_monitoring(cf):
    """
    creates dictionaries, where train/val metrics are stored.
    """
    metrics = {}
    # first entry for loss dict accounts for epoch starting at 1.
    metrics['train'] = OrderedDict()
    metrics['val'] = OrderedDict()
    metric_classes = []
    if 'rois' in cf.report_score_level:
        metric_classes.extend([v for k, v in cf.class_dict.items()])
    if 'patient' in cf.report_score_level:
        metric_classes.extend(['patient'])
    for cl in metric_classes:
        metrics['train'][cl + '_ap'] = [None]
        metrics['val'][cl + '_ap'] = [None]
        if cl == 'patient':
            metrics['train'][cl + '_auc'] = [None]
            metrics['val'][cl + '_auc'] = [None]

    metrics['train']['monitor_values'] = [[] for _ in range(cf.num_epochs + 1)]
    metrics['val']['monitor_values'] = [[] for _ in range(cf.num_epochs + 1)]

    # generate isntance of monitor plot class.
    TrainingPlot = plotting.TrainingPlot_2Panel(cf)

    return metrics, TrainingPlot



def create_csv_output(cf, logger, results_list):

    logger.info('creating csv output file at {}'.format(os.path.join(cf.exp_dir, 'output.csv')))
    submission_df = pd.DataFrame(columns=['patientID', 'PredictionString'])
    for r in results_list:
        pid = r[1]
        prediction_string = ''
        for box in r[0][0]:
            coords = box['box_coords']
            score = box['box_score']
            pred_class = box['box_pred_class_id']

            if score >= cf.min_det_thresh:
                x = coords[1] #* cf.pp_downsample_factor
                y = coords[0] #* cf.pp_downsample_factor
                width = (coords[3] - coords[1]) #* cf.pp_downsample_factor
                height = (coords[2] - coords[0]) #* cf.pp_downsample_factor
                if len(coords) == 6:
                    z = coords[4]
                    depth = (coords[5] - coords[4])
                    prediction_string += '{} {} {} {} {} {} {} {}'.format(score, pred_class, x, y, z, width, height, depth)
                else:
                    prediction_string += '{} {} {} {} {} {} '.format(score, pred_class, x, y, width, height)

        if prediction_string == '':
            prediction_string = None
        submission_df.loc[len(submission_df)] = [pid, prediction_string]
    submission_df.to_csv(os.path.join(cf.exp_dir, 'output.csv'), index=False)



class _AnsiColorizer(object):
    """
    A colorizer is an object that loosely wraps around a stream, allowing
    callers to write text to the stream in a particular color.

    Colorizer classes must implement C{supported()} and C{write(text, color)}.
    """
    _colors = dict(black=30, red=31, green=32, yellow=33,
                   blue=34, magenta=35, cyan=36, white=37, default=39)

    def __init__(self, stream):
        self.stream = stream

    @classmethod
    def supported(cls, stream=sys.stdout):
        """
        A class method that returns True if the current platform supports
        coloring terminal output using this method. Returns False otherwise.
        """
        if not stream.isatty():
            return False  # auto color only on TTYs
        try:
            import curses
        except ImportError:
            return False
        else:
            try:
                try:
                    return curses.tigetnum("colors") > 2
                except curses.error:
                    curses.setupterm()
                    return curses.tigetnum("colors") > 2
            except:
                raise
                # guess false in case of error
                return False

    def write(self, text, color):
        """
        Write the given text to the stream in the given color.

        @param text: Text to be written to the stream.

        @param color: A string label for a color. e.g. 'red', 'white'.
        """
        color = self._colors[color]
        self.stream.write('\x1b[%sm%s\x1b[0m' % (color, text))



class ColorHandler(logging.StreamHandler):


    def __init__(self, stream=sys.stdout):
        super(ColorHandler, self).__init__(_AnsiColorizer(stream))

    def emit(self, record):
        msg_colors = {
            logging.DEBUG: "green",
            logging.INFO: "default",
            logging.WARNING: "red",
            logging.ERROR: "red"
        }
        color = msg_colors.get(record.levelno, "blue")
        self.stream.write(record.msg + "\n", color)

