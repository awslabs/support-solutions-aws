# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ruff: noqa: D101, D102, D103
"""Tests for the AWS Helper."""

import os
from well_architected_review_mcp_server_for_sagemaker import __version__
from well_architected_review_mcp_server_for_sagemaker.aws_helper import AwsHelper
from unittest.mock import ANY, MagicMock, patch


class TestAwsHelper:

    def setup_method(self):
        AwsHelper._client_cache = {}
        AwsHelper._cache_metadata = {}

    @patch.dict(os.environ, {'AWS_REGION': 'us-west-2'})
    def test_get_aws_region_from_env(self):
        assert AwsHelper.get_aws_region() == 'us-west-2'

    @patch.dict(os.environ, {}, clear=True)
    def test_get_aws_region_default(self):
        assert AwsHelper.get_aws_region() is None

    @patch.dict(os.environ, {'AWS_PROFILE': 'test-profile'})
    def test_get_aws_profile_from_env(self):
        assert AwsHelper.get_aws_profile() == 'test-profile'

    @patch.dict(os.environ, {}, clear=True)
    def test_get_aws_profile_none(self):
        assert AwsHelper.get_aws_profile() is None

    @patch('boto3.client')
    def test_create_client_no_profile_with_region(self, mock_boto3_client):
        with patch.object(AwsHelper, 'get_aws_profile', return_value=None):
            with patch.object(AwsHelper, 'get_aws_region', return_value='us-west-2'):
                AwsHelper.create_boto3_client('sagemaker')
                mock_boto3_client.assert_called_once_with(
                    'sagemaker', region_name='us-west-2', config=ANY
                )

    @patch('boto3.client')
    def test_create_client_no_profile_no_region(self, mock_boto3_client):
        with patch.object(AwsHelper, 'get_aws_profile', return_value=None):
            with patch.object(AwsHelper, 'get_aws_region', return_value=None):
                AwsHelper.create_boto3_client('sagemaker')
                mock_boto3_client.assert_called_once_with('sagemaker', config=ANY)

    @patch('boto3.Session')
    def test_create_client_with_profile(self, mock_session_cls):
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        with patch.object(AwsHelper, 'get_aws_profile', return_value='test-profile'):
            with patch.object(AwsHelper, 'get_aws_region', return_value='us-west-2'):
                AwsHelper.create_boto3_client('sagemaker')
                mock_session_cls.assert_called_once_with(profile_name='test-profile')
                mock_session.client.assert_called_once_with(
                    'sagemaker', region_name='us-west-2', config=ANY
                )

    @patch('boto3.client')
    def test_create_client_region_override(self, mock_boto3_client):
        with patch.object(AwsHelper, 'get_aws_profile', return_value=None):
            AwsHelper.create_boto3_client('sagemaker', region_name='us-west-2')
            mock_boto3_client.assert_called_once_with(
                'sagemaker', region_name='us-west-2', config=ANY
            )

    def test_user_agent_suffix(self):
        with patch.object(AwsHelper, 'get_aws_profile', return_value=None):
            with patch.object(AwsHelper, 'get_aws_region', return_value=None):
                with patch('boto3.client') as mock_client:
                    AwsHelper.create_boto3_client('sagemaker')
                    _, kwargs = mock_client.call_args
                    config = kwargs.get('config')
                    assert config is not None
                    expected = f'md/well-architected-review-mcp-server-for-sagemaker#{__version__}'
                    assert config.user_agent_extra == expected

    @patch('boto3.client')
    def test_client_caching(self, mock_boto3_client):
        mock_client = MagicMock()
        mock_boto3_client.return_value = mock_client
        with patch.object(AwsHelper, 'get_aws_profile', return_value=None):
            with patch.object(AwsHelper, 'get_aws_region', return_value='us-west-2'):
                client1 = AwsHelper.create_boto3_client('sagemaker')
                client2 = AwsHelper.create_boto3_client('sagemaker')
                mock_boto3_client.assert_called_once()
                assert client1 is client2

    @patch('boto3.client')
    def test_client_caching_different_regions(self, mock_boto3_client):
        mock_c1 = MagicMock()
        mock_c2 = MagicMock()
        mock_boto3_client.side_effect = [mock_c1, mock_c2]
        with patch.object(AwsHelper, 'get_aws_profile', return_value=None):
            with patch.object(AwsHelper, 'get_aws_region', return_value=None):
                c1 = AwsHelper.create_boto3_client('sagemaker', 'us-east-1')
                c2 = AwsHelper.create_boto3_client('sagemaker', 'us-west-2')
                assert c1 is not c2
                assert mock_boto3_client.call_count == 2
