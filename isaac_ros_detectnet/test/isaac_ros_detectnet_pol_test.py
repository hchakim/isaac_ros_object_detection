# Copyright (c) 2022, NVIDIA CORPORATION.  All rights reserved.
#
# NVIDIA CORPORATION and its licensors retain all intellectual property
# and proprietary rights in and to this software, related documentation
# and any modifications thereto.  Any use, reproduction, disclosure or
# distribution of this software and related documentation without an express
# license agreement from NVIDIA CORPORATION is strictly prohibited.

"""
Proof-Of-Life test for the Isaac ROS DetectNet package.

    1. Sets up DnnImageEncoderNode, TensorRTNode, DetectNetDecoderNode
    2. Loads a sample image and publishes it
    3. Subscribes to the relevant topics, waiting for an output from DetectNetDecoderNode
    4. Verifies that the received output sizes and encodings are correct (based on dummy model)

    Note: the data is not verified because the model is initialized with random weights
"""


import os
import pathlib
from pprint import pprint
import subprocess
import time

from isaac_ros_test import IsaacROSBaseTest, JSONConversion
from launch_ros.actions.composable_node_container import ComposableNodeContainer
from launch_ros.descriptions.composable_node import ComposableNode

import pytest
import rclpy

from sensor_msgs.msg import Image
from vision_msgs.msg import Detection2DArray

_TEST_CASE_NAMESPACE = 'detectnet_node_test'


@pytest.mark.rostest
def generate_test_description():
    """Generate launch description for testing relevant nodes."""
    launch_dir_path = os.path.dirname(os.path.realpath(__file__))
    model_dir_path = launch_dir_path + '/dummy_model'
    model_name = 'detectnet'
    model_version = 1
    engine_file_path = f'{model_dir_path}/{model_name}/{model_version}/model.plan'

    # Read labels from text file
    labels_file_path = f'{model_dir_path}/{model_name}/labels.txt'
    with open(labels_file_path, 'r') as fd:
        label_list = fd.read().strip().splitlines()

    # Generate engine file using tao-converter
    if not os.path.isfile(engine_file_path):
        tao_converter_args = [
            '-k', '"object-detection-from-sim-pipeline"',
            '-d', '3,368,640',
            '-t', 'fp16',
            '-p', 'input_1,1x3x368x640,1x3x368x640,1x3x368x640',
            '-e', engine_file_path,
            '-o', 'output_cov/Sigmoid,output_bbox/BiasAdd',
            f'{model_dir_path}/{model_name}/1/resnet18_detector.etlt'
        ]
        tao_converter_executable = '/opt/nvidia/tao/tao-converter'
        print('Running command:\n' + ' '.join([tao_converter_executable] + tao_converter_args))

        result = subprocess.run(
            [tao_converter_executable] + tao_converter_args,
            env=os.environ,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        if result.returncode != 0:
            raise Exception(
                f'Failed to convert with status: {result.returncode}.\n'
                f'stderr:\n' + result.stderr.decode('utf-8')
            )

    encoder_node = ComposableNode(
        name='DnnImageEncoderNode',
        package='isaac_ros_dnn_encoders',
        plugin='nvidia::isaac_ros::dnn_inference::DnnImageEncoderNode',
        namespace=IsaacROSDetectNetPipelineTest.generate_namespace(_TEST_CASE_NAMESPACE),
        parameters=[{
            'network_image_width': 640,
            'network_image_height': 368
        }],
        remappings=[('encoded_tensor', 'tensor_pub')]
    )

    triton_node = ComposableNode(
        name='TritonNode',
        package='isaac_ros_triton',
        namespace=IsaacROSDetectNetPipelineTest.generate_namespace(_TEST_CASE_NAMESPACE),
        plugin='nvidia::isaac_ros::dnn_inference::TritonNode',
        parameters=[{
            'model_name': 'detectnet',
            'model_repository_paths': [model_dir_path],
            'input_tensor_names': ['input_tensor'],
            'input_binding_names': ['input_1'],
            'input_tensor_formats': ['nitros_tensor_list_nchw_rgb_f32'],
            'output_tensor_names': ['output_cov', 'output_bbox'],
            'output_binding_names': ['output_cov/Sigmoid', 'output_bbox/BiasAdd'],
            'output_tensor_formats': ['nitros_tensor_list_nhwc_rgb_f32'],
            'log_level': 0
        }])

    detectnet_decoder_node = ComposableNode(
        name='DetectNetDecoderNode',
        package='isaac_ros_detectnet',
        plugin='nvidia::isaac_ros::detectnet::DetectNetDecoderNode',
        namespace=IsaacROSDetectNetPipelineTest.generate_namespace(_TEST_CASE_NAMESPACE),
        parameters=[{
            'frame_id': 'detectnet',
            'label_names': label_list,
            'coverage_threshold': 0.5,
            'bounding_box_scale': 35.0,
            'bounding_box_offset': 0.5,
            'eps': 0.5,
            'min_boxes': 2,
            'verbose': False,
        }])

    container = ComposableNodeContainer(
        name='detectnet_container',
        namespace='',
        package='rclcpp_components',
        executable='component_container_mt',
        composable_node_descriptions=[
            triton_node,
            encoder_node,
            detectnet_decoder_node
        ],
        output='screen'
    )

    return IsaacROSDetectNetPipelineTest.generate_test_description([container])


class IsaacROSDetectNetPipelineTest(IsaacROSBaseTest):
    """Validates a DetectNet model with randomized weights with a sample output from Python."""

    filepath = pathlib.Path(os.path.dirname(__file__))
    MODEL_GENERATION_TIMEOUT_SEC = 60
    INIT_WAIT_SEC = 1
    MODEL_PATH = filepath.joinpath('dummy_model/detectnet.engine')

    @IsaacROSBaseTest.for_each_test_case()
    def test_image_detection(self, test_folder):
        start_time = time.time()

        while (time.time() - start_time) < self.MODEL_GENERATION_TIMEOUT_SEC:
            time.sleep(self.INIT_WAIT_SEC)

        self.node._logger.info('Starting to test')

        """Expect the node to segment an image."""
        self.generate_namespace_lookup(
            ['image', 'detectnet/detections'], _TEST_CASE_NAMESPACE)
        image_pub = self.node.create_publisher(Image, self.namespaces['image'], self.DEFAULT_QOS)
        received_messages = {}
        detectnet_detections = self.create_logging_subscribers(
            [('detectnet/detections', Detection2DArray)],
            received_messages, accept_multiple_messages=False)

        self.generate_namespace_lookup(
            ['image', 'detectnet/detections'], _TEST_CASE_NAMESPACE)

        try:
            image = JSONConversion.load_image_from_json(test_folder / 'detections.json')
            ground_truth = open(test_folder.joinpath('expected_detections.txt'), 'r')
            expected_detections = []

            for ground_detection in ground_truth.readlines():
                ground_detection_split = ground_detection.split()
                gtd = [float(ground_detection_split[4]), float(ground_detection_split[5]),
                       float(ground_detection_split[6]), float(ground_detection_split[7])]
                expected_detections.append(
                    {'width': int(gtd[2] - gtd[0]), 'height': int(gtd[3] - gtd[1]),
                        'center': {'x': int((gtd[2]+gtd[0])/2), 'y': int((gtd[3]+gtd[1])/2)}}
                )

            TIMEOUT = 60
            end_time = time.time() + TIMEOUT
            done = False
            while time.time() < end_time:
                image_pub.publish(image)
                rclpy.spin_once(self.node, timeout_sec=0.1)

                if 'detectnet/detections' in received_messages:
                    pprint(received_messages['detectnet/detections'].detections[0])
                    done = True
                    break

            self.assertTrue(
                done, "Didn't receive output on detectnet/detections topic!")

            detection_list = received_messages['detectnet/detections'].detections

            pixel_tolerance = 2.0
            self.assertEqual(pytest.approx(detection_list[0].bbox.size_x, pixel_tolerance),
                             expected_detections[0]['width'], 'Received incorrect width')
            self.assertEqual(pytest.approx(detection_list[0].bbox.size_y, pixel_tolerance),
                             expected_detections[0]['height'], 'Received incorrect height')
            self.assertEqual(
                pytest.approx(detection_list[0].bbox.center.position.x, pixel_tolerance),
                expected_detections[0]['center']['x'], 'Received incorrect center')
            self.assertEqual(
                pytest.approx(detection_list[0].bbox.center.position.y, pixel_tolerance),
                expected_detections[0]['center']['y'], 'Received incorrect center')
        finally:
            self.node.destroy_subscription(detectnet_detections)
            self.node.destroy_publisher(image_pub)
