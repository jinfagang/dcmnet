from __future__ import print_function
import sys
import os
import pickle
import argparse
import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
import torchvision.transforms as transforms
import numpy as np
from torch.autograd import Variable

from datasets.data_augment import BaseTransform, preproc
from datasets.config import VOCroot, COCOroot, VOC_300, VOC_512, COCO_300, COCO_512, COCO_mobile_300
from datasets.voc0712 import VOCDetection, VOC_CLASSES, detection_collate, AnnotationTransform
from datasets.coco import COCODetection

import torch.utils.data as data
from layers.functions import Detect, PriorBox
from utils.nms_wrapper import nms
from utils.timer import Timer
from alfred.utils.log import logger as logging
from alfred.vis.image.common import get_unique_color_by_id
import cv2


#os.environ["CUDA_VISIBLE_DEVICES"] = "0"

parser = argparse.ArgumentParser(description='Receptive Field Block Net')

parser.add_argument('-v', '--version', default='RFB_d3',
                    help='RFB_vgg ,RFB_E_vgg or RFB_mobile | RFB_d2 | RFB_d3 | RFB_d4 | RFB_d4_fpn')
parser.add_argument('-s', '--size', default='512',
                    help='300 or 512 input size.')
parser.add_argument('-d', '--dataset', default='VOC',
                    help='VOC_783 | VOC_2392 | VOC_8783 | VOC | COCO')
parser.add_argument('-m', '--trained_model', default='./weights/2392Final_RFB_d3_VOC.pth',
                    type=str, help='Trained state_dict file path to open')
parser.add_argument('--save_folder', default='eval/', type=str,
                    help='Dir to save results')
parser.add_argument('--cuda', default=True, type=bool,
                    help='Use cuda to train model')
parser.add_argument('--cpu', default=False, type=bool,
                    help='Use cpu nms')
parser.add_argument('--retest', default=False, type=bool,
                    help='test cache results')
args = parser.parse_args()

if not os.path.exists(args.save_folder):
    os.mkdir(args.save_folder)

if args.dataset == 'VOC':
    logging.info('using dataset: {}, size: {}'.format(args.dataset, args.size))
    cfg = (VOC_300, VOC_512)[args.size == '512']
else:
    cfg = (COCO_300, COCO_512)[args.size == '512']

if args.version == 'RFB_vgg':
    from models.RFB_Net_vgg import build_net
    logging.info('building {} model'.format('RFB_vgg'))
elif args.version == 'RFB_E_vgg':
    from models.RFB_Net_E_vgg import build_net
    logging.info('building {} model'.format('RFB_E_vgg'))
elif args.version == 'RFB_d2':
    from models.RFB_Net_vgg_d2 import build_net
elif args.version == 'RFB_d3':
    from models.RFB_Net_vgg_d3 import build_net
elif args.version == 'RFB_d4':
    from models.RFB_Net_vgg_d4 import build_net
elif args.version == 'RFB_d4_fpn':
    from models.RFB_Net_vgg_d4_fpn import build_net
elif args.version == 'RFB_mobile':
    from models.RFB_Net_mobile import build_net
    cfg = COCO_mobile_300
else:
    print('Unkown version!')

priorbox = PriorBox(cfg)
with torch.no_grad():
    priors = priorbox.forward()
    if args.cuda:
        priors = priors.cuda()


def test_net(save_folder, net, detector, cuda, testset, transform, vis=True, max_per_image=300, thresh=0.005):

    if not os.path.exists(save_folder):
        os.mkdir(save_folder)
    # dump predictions and assoc. ground truth to text file for now
    num_images = len(testset)
    # 738:6 classes ; 2392:7 ; 8718:6
    num_classes = (21, 81)[args.dataset == 'COCO']
    all_boxes = [[[] for _ in range(num_images)]
                 for _ in range(num_classes)]

    _t = {'im_detect': Timer(), 'misc': Timer()}
    det_file = os.path.join(save_folder, 'detections.pkl')

    if args.retest:
        f = open(det_file, 'rb')
        all_boxes = pickle.load(f)
        print('Evaluating detections')
        testset.evaluate_detections(all_boxes, save_folder)
        return

    for i in range(num_images):
        # name, img = testset.pull_image(i)#测试
        img = testset.pull_image(i)
        # print(name)
        scale = torch.Tensor([img.shape[1], img.shape[0],
                              img.shape[1], img.shape[0]])
        with torch.no_grad():
            x = transform(img).unsqueeze(0)
            if cuda:
                x = x.cuda()
                scale = scale.cuda()

        _t['im_detect'].tic()
        out = net(x)      # forward pass
        boxes, scores = detector.forward(out, priors)
        detect_time = _t['im_detect'].toc()
        boxes = boxes[0]
        scores = scores[0]

        boxes *= scale
        boxes = boxes.cpu().numpy()
        scores = scores.cpu().numpy()
        # scale each detection back up to the image

        _t['misc'].tic()

        font = cv2.FONT_HERSHEY_SIMPLEX
        font_scale = 0.36

        font_thickness = 1
        line_thickness = 1

        for j in range(1, num_classes):
            inds = np.where(scores[:, j] > thresh)[0]
            if len(inds) == 0:
                all_boxes[j][i] = np.empty([0, 5], dtype=np.float32)
                continue
            c_bboxes = boxes[inds]
            c_scores = scores[inds, j]
            c_dets = np.hstack((c_bboxes, c_scores[:, np.newaxis])).astype(
                np.float32, copy=False)

            keep = nms(c_dets, 0.45, force_cpu=False)
            c_dets = c_dets[keep, :]
            all_boxes[j][i] = c_dets

            unique_color = get_unique_color_by_id(j)
            if vis:
                for i in range(len(inds)):
                    bb = c_bboxes[i]
                    score = c_scores[i]

                    y1 = int(bb[0])
                    x1 = int(bb[1])
                    y2 = int(bb[2])
                    x2 = int(bb[3])

                    cv2.rectangle(img, (x1, y1), (x2, y2),
                                unique_color, line_thickness)

                    text_label = '{}'.format(j)
                    (ret_val, base_line) = cv2.getTextSize(
                        text_label, font, font_scale, font_thickness)
                    text_org = (x1, y1 - 0)

                    cv2.rectangle(img, (text_org[0] - 5, text_org[1] + base_line + 2),
                                (text_org[0] + ret_val[0] + 5, text_org[1] - ret_val[1] - 2), unique_color, line_thickness)
                    # this rectangle for fill text rect
                    cv2.rectangle(img, (text_org[0] - 5, text_org[1] + base_line + 2),
                                (text_org[0] + ret_val[0] + 4,
                                text_org[1] - ret_val[1] - 2),
                                unique_color, -1)
                    cv2.putText(img, text_label, text_org, font,
                                font_scale, (255, 255, 255), font_thickness)

        # we can vis det result here
        logging.info('all boxes: {}'.format(all_boxes))
        if vis:
            cv2.imshow('rr', img)
            cv2.waitKey(0)
            
        if max_per_image > 0:
            image_scores = np.hstack([all_boxes[j][i][:, -1]
                                      for j in range(1, num_classes)])
            if len(image_scores) > max_per_image:
                image_thresh = np.sort(image_scores)[-max_per_image]
                for j in range(1, num_classes):
                    keep = np.where(all_boxes[j][i][:, -1] >= image_thresh)[0]
                    all_boxes[j][i] = all_boxes[j][i][keep, :]

                    # visualize the box here
                    logging.info('start vis, all_boxes: {}'.format(all_boxes))
                    

        nms_time = _t['misc'].toc()

        if i % 20 == 0:
            print('im_detect: {:d}/{:d} {:.3f}s {:.3f}s'
                  .format(i + 1, num_images, detect_time, nms_time))
            _t['im_detect'].clear()
            _t['misc'].clear()

    with open(det_file, 'wb') as f:
        pickle.dump(all_boxes, f, pickle.HIGHEST_PROTOCOL)

    print('Evaluating detections')
    testset.evaluate_detections(all_boxes, save_folder)


if __name__ == '__main__':
    # load net
    img_dim = (300, 512)[args.size == '512']
    # 738:6 classes ; 2392:7 ; 8718:6
    num_classes = (21, 81)[args.dataset == 'COCO']
    net = build_net('test', img_dim, num_classes)    # initialize detector

    logging.info('load model from: {}'.format(args.trained_model))
    state_dict = torch.load(args.trained_model)
    # create new OrderedDict that does not contain `module.`

    from collections import OrderedDict
    new_state_dict = OrderedDict()
    for k, v in state_dict.items():
        head = k[:7]
        if head == 'module.':
            name = k[7:]  # remove `module.`
        else:
            name = k
        new_state_dict[name] = v
    # print(net)

    # net = nn.DataParallel(net)#后来加的
    net.load_state_dict(new_state_dict)
    net.eval()
    print('Finished loading model!')
    # print(net)
    # load data
    if args.dataset == 'VOC':
        testset = VOCDetection(
            VOCroot, [('2007', 'test')], None, AnnotationTransform())
    elif args.dataset == 'COCO':
        testset = COCODetection(
            COCOroot, [('2014', 'minival')], None)
        # COCOroot, [('2015', 'test-dev')], None)
    else:
        print('Only VOC and COCO dataset are supported now!')
    if args.cuda:
        net = net.cuda()
        cudnn.benchmark = True
    else:
        net = net.cpu()
    # evaluation
    #top_k = (300, 200)[args.dataset == 'COCO']
    #top_k = (300, 200)[args.dataset == 'VOC']
    top_k = 200
    detector = Detect(num_classes, 0, cfg)
    save_folder = os.path.join(args.save_folder, args.dataset)
    rgb_means = ((104, 117, 123), (103.94, 116.78, 123.68))[
        args.version == 'RFB_mobile']
    #rgb_means = ((104, 117, 123),(103.94,116.78,123.68))[args.version == 'RFB_vgg']
    test_net(save_folder, net, detector, args.cuda, testset,
             BaseTransform(net.size, rgb_means, (2, 0, 1)),
             top_k, thresh=0.01)
