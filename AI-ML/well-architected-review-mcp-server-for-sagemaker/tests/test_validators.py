# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ruff: noqa: D101, D102, D103
"""Tests for the Well-Architected pillar validators."""

from well_architected_review_mcp_server_for_sagemaker.validators.security import validate_security
from well_architected_review_mcp_server_for_sagemaker.validators.reliability import validate_reliability
from well_architected_review_mcp_server_for_sagemaker.validators.performance import validate_performance
from well_architected_review_mcp_server_for_sagemaker.validators.cost import validate_cost
from well_architected_review_mcp_server_for_sagemaker.validators.operational_excellence import (
    validate_operational_excellence,
)
from well_architected_review_mcp_server_for_sagemaker.validators.sustainability import validate_sustainability
from well_architected_review_mcp_server_for_sagemaker.validators import run_all_validators
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------


class TestSecurityValidator:

    def test_no_iam_role(self):
        info = {'EndpointName': 'ep-1'}
        findings = validate_security('endpoint', info, [])
        checks = [f['check'] for f in findings]
        assert 'no-iam-role' in checks

    def test_iam_role_present(self):
        info = {'EndpointName': 'ep-1', 'RoleArn': 'arn:aws:iam::123:role/SageMakerRole'}
        with patch('well_architected_review_mcp_server_for_sagemaker.validators.security.AwsHelper') as mock_aws:
            mock_iam = MagicMock()
            mock_iam.list_attached_role_policies.return_value = {'AttachedPolicies': []}
            mock_aws.create_boto3_client.return_value = mock_iam
            findings = validate_security('endpoint', info, [])
            checks = [f['check'] for f in findings]
            assert 'no-iam-role' not in checks

    def test_overly_permissive_role(self):
        info = {'EndpointName': 'ep-1', 'RoleArn': 'arn:aws:iam::123:role/AdminRole'}
        with patch('well_architected_review_mcp_server_for_sagemaker.validators.security.AwsHelper') as mock_aws:
            mock_iam = MagicMock()
            mock_iam.list_attached_role_policies.return_value = {
                'AttachedPolicies': [{'PolicyName': 'AdministratorAccess'}]
            }
            mock_aws.create_boto3_client.return_value = mock_iam
            findings = validate_security('endpoint', info, [])
            checks = [f['check'] for f in findings]
            assert 'overly-permissive-role' in checks

    def test_vpc_isolation_missing(self):
        info = {'EndpointName': 'ep-1'}
        findings = validate_security('endpoint', info, [])
        checks = [f['check'] for f in findings]
        assert 'vpc-isolation' in checks

    def test_vpc_isolation_present(self):
        info = {'EndpointName': 'ep-1', 'VpcConfig': {'Subnets': ['subnet-1']}}
        findings = validate_security('endpoint', info, [])
        checks = [f['check'] for f in findings]
        assert 'vpc-isolation' not in checks

    def test_notebook_direct_internet(self):
        info = {
            'NotebookInstanceName': 'nb-1',
            'SubnetId': 'subnet-1',
            'DirectInternetAccess': 'Enabled',
        }
        findings = validate_security('notebook_instance', info, [])
        checks = [f['check'] for f in findings]
        assert 'direct-internet-access' in checks

    def test_notebook_root_access(self):
        info = {
            'NotebookInstanceName': 'nb-1',
            'SubnetId': 'subnet-1',
            'DirectInternetAccess': 'Disabled',
            'RootAccess': 'Enabled',
        }
        findings = validate_security('notebook_instance', info, [])
        checks = [f['check'] for f in findings]
        assert 'root-access' in checks

    def test_inter_container_encryption(self):
        info = {
            'TrainingJobName': 'tj-1',
            'EnableInterContainerTrafficEncryption': False,
        }
        findings = validate_security('training_job', info, [])
        checks = [f['check'] for f in findings]
        assert 'inter-container-encryption' in checks

    def test_network_isolation(self):
        info = {'TrainingJobName': 'tj-1', 'EnableNetworkIsolation': False}
        findings = validate_security('training_job', info, [])
        checks = [f['check'] for f in findings]
        assert 'network-isolation' in checks


# ---------------------------------------------------------------------------
# Reliability
# ---------------------------------------------------------------------------


class TestReliabilityValidator:

    def test_single_instance_endpoint(self):
        info = {'EndpointName': 'ep-1', 'EndpointConfigName': 'cfg-1'}
        with patch(
            'well_architected_review_mcp_server_for_sagemaker.validators.reliability.AwsHelper'
        ) as mock_aws:
            mock_sm = MagicMock()
            mock_sm.describe_endpoint_config.return_value = {
                'ProductionVariants': [
                    {'VariantName': 'AllTraffic', 'InitialInstanceCount': 1}
                ]
            }
            mock_aas = MagicMock()
            mock_aas.describe_scalable_targets.return_value = {'ScalableTargets': []}
            mock_aas.describe_scaling_policies.return_value = {'ScalingPolicies': []}
            mock_sq = MagicMock()
            mock_sq.get_service_quota.return_value = {'Quota': {'Value': 20}}
            mock_s3 = MagicMock()

            def side_effect(service, **kwargs):
                if service == 'sagemaker':
                    return mock_sm
                if service == 'application-autoscaling':
                    return mock_aas
                if service == 'service-quotas':
                    return mock_sq
                if service == 's3':
                    return mock_s3
                return MagicMock()

            mock_aws.create_boto3_client.side_effect = side_effect
            findings = validate_reliability('endpoint', info)
            checks = [f['check'] for f in findings]
            assert 'single-instance-endpoint' in checks
            assert 'no-multi-az' in checks

    def test_training_no_checkpointing(self):
        info = {
            'TrainingJobName': 'tj-1',
            'StoppingCondition': {'MaxRuntimeInSeconds': 3600},
        }
        findings = validate_reliability('training_job', info)
        checks = [f['check'] for f in findings]
        assert 'no-checkpointing' in checks
        assert 'no-retry-strategy' in checks

    def test_training_timeout_too_long(self):
        info = {
            'TrainingJobName': 'tj-1',
            'StoppingCondition': {'MaxRuntimeInSeconds': 999999},
        }
        findings = validate_reliability('training_job', info)
        checks = [f['check'] for f in findings]
        assert 'training-timeout' in checks


# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------


class TestPerformanceValidator:

    def test_older_instance_generation(self):
        info = {'EndpointName': 'ep-1', 'EndpointConfigName': 'cfg-1'}
        with patch(
            'well_architected_review_mcp_server_for_sagemaker.validators.performance.AwsHelper'
        ) as mock_aws:
            mock_sm = MagicMock()
            mock_sm.describe_endpoint_config.return_value = {
                'ProductionVariants': [
                    {'VariantName': 'v1', 'InstanceType': 'ml.m4.xlarge'}
                ],
            }
            mock_cw = MagicMock()
            mock_cw.get_metric_statistics.return_value = {'Datapoints': []}

            def side_effect(service, **kwargs):
                if service == 'sagemaker':
                    return mock_sm
                if service == 'cloudwatch':
                    return mock_cw
                return MagicMock()

            mock_aws.create_boto3_client.side_effect = side_effect
            findings = validate_performance('endpoint', info)
            checks = [f['check'] for f in findings]
            assert 'older-instance-generation' in checks

    def test_training_data_distribution(self):
        info = {
            'TrainingJobName': 'tj-1',
            'ResourceConfig': {'InstanceType': 'ml.m5.xlarge', 'InstanceCount': 4},
            'InputDataConfig': [
                {
                    'ChannelName': 'train',
                    'DataSource': {
                        'S3DataSource': {'S3DataDistributionType': 'FullyReplicated'}
                    },
                }
            ],
        }
        findings = validate_performance('training_job', info)
        checks = [f['check'] for f in findings]
        assert 'data-distribution' in checks


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


class TestCostValidator:

    def test_no_spot_training(self):
        info = {'TrainingJobName': 'tj-1', 'EnableManagedSpotTraining': False}
        findings = validate_cost('training_job', info, [])
        checks = [f['check'] for f in findings]
        assert 'no-spot-training' in checks

    def test_spot_training_enabled(self):
        info = {'TrainingJobName': 'tj-1', 'EnableManagedSpotTraining': True}
        findings = validate_cost('training_job', info, [])
        checks = [f['check'] for f in findings]
        assert 'no-spot-training' not in checks

    def test_running_notebook(self):
        info = {
            'NotebookInstanceName': 'nb-1',
            'NotebookInstanceStatus': 'InService',
        }
        findings = validate_cost('notebook_instance', info, [])
        checks = [f['check'] for f in findings]
        assert 'running-notebook' in checks

    def test_no_lifecycle_config(self):
        info = {'NotebookInstanceName': 'nb-1', 'NotebookInstanceStatus': 'Stopped'}
        findings = validate_cost('notebook_instance', info, [])
        checks = [f['check'] for f in findings]
        assert 'no-lifecycle-config' in checks


# ---------------------------------------------------------------------------
# Operational Excellence
# ---------------------------------------------------------------------------


class TestOperationalExcellenceValidator:

    def test_no_autoscaling(self):
        info = {'EndpointName': 'ep-1'}
        with patch(
            'well_architected_review_mcp_server_for_sagemaker.validators.operational_excellence.AwsHelper'
        ) as mock_aws:
            mock_aas = MagicMock()
            mock_aas.describe_scalable_targets.return_value = {'ScalableTargets': []}
            mock_cw = MagicMock()
            mock_cw.list_metrics.return_value = {'Metrics': [{'MetricName': 'Invocations'}]}
            mock_cw.describe_alarms_for_metric.return_value = {'MetricAlarms': []}
            mock_logs = MagicMock()
            mock_logs.describe_log_groups.return_value = {'logGroups': []}

            def side_effect(service, **kwargs):
                if service == 'application-autoscaling':
                    return mock_aas
                if service == 'cloudwatch':
                    return mock_cw
                if service == 'logs':
                    return mock_logs
                return MagicMock()

            mock_aws.create_boto3_client.side_effect = side_effect
            findings = validate_operational_excellence('endpoint', info, [])
            checks = [f['check'] for f in findings]
            assert 'no-autoscaling' in checks

    def test_experiment_tracking_missing(self):
        info = {'TrainingJobName': 'tj-1'}
        with patch(
            'well_architected_review_mcp_server_for_sagemaker.validators.operational_excellence.AwsHelper'
        ) as mock_aws:
            mock_logs = MagicMock()
            mock_logs.describe_log_groups.return_value = {'logGroups': []}
            mock_aws.create_boto3_client.return_value = mock_logs
            findings = validate_operational_excellence('training_job', info, [])
            checks = [f['check'] for f in findings]
            assert 'no-experiment-tracking' in checks


# ---------------------------------------------------------------------------
# Sustainability
# ---------------------------------------------------------------------------


class TestSustainabilityValidator:

    def test_graviton_suggestion_training(self):
        info = {
            'TrainingJobName': 'tj-1',
            'ResourceConfig': {'InstanceType': 'ml.m5.xlarge'},
        }
        findings = validate_sustainability('training_job', info, [])
        checks = [f['check'] for f in findings]
        assert 'consider-graviton' in checks

    def test_oversized_notebook(self):
        info = {
            'NotebookInstanceName': 'nb-1',
            'InstanceType': 'ml.m5.4xlarge',
            'NotebookInstanceStatus': 'InService',
        }
        findings = validate_sustainability('notebook_instance', info, [])
        checks = [f['check'] for f in findings]
        assert 'oversized-notebook' in checks


# ---------------------------------------------------------------------------
# run_all_validators integration
# ---------------------------------------------------------------------------


class TestRunAllValidators:

    def test_returns_findings_from_all_pillars(self):
        info = {
            'EndpointName': 'ep-1',
            'EndpointConfigName': 'cfg-1',
        }
        # Create a mock that returns safe defaults for all boto3 calls
        mock_client = MagicMock()
        mock_client.describe_endpoint_config.return_value = {
            'ProductionVariants': [{'VariantName': 'v1', 'InitialInstanceCount': 1}],
        }
        mock_client.list_metrics.return_value = {'Metrics': []}
        mock_client.describe_alarms_for_metric.return_value = {'MetricAlarms': []}
        mock_client.describe_log_groups.return_value = {'logGroups': []}
        mock_client.describe_scalable_targets.return_value = {'ScalableTargets': []}
        mock_client.describe_scaling_policies.return_value = {'ScalingPolicies': []}
        mock_client.get_service_quota.return_value = {'Quota': {'Value': 20}}
        mock_client.get_metric_statistics.return_value = {'Datapoints': []}
        mock_client.list_attached_role_policies.return_value = {'AttachedPolicies': []}
        mock_client.describe_trails.return_value = {'trailList': []}
        mock_client.describe_configuration_recorder_status.return_value = {
            'ConfigurationRecordersStatus': []
        }
        mock_client.describe_model.return_value = {'PrimaryContainer': {}}

        with patch(
            'well_architected_review_mcp_server_for_sagemaker.validators.security.AwsHelper'
        ) as m1, patch(
            'well_architected_review_mcp_server_for_sagemaker.validators.reliability.AwsHelper'
        ) as m2, patch(
            'well_architected_review_mcp_server_for_sagemaker.validators.performance.AwsHelper'
        ) as m3, patch(
            'well_architected_review_mcp_server_for_sagemaker.validators.cost.AwsHelper'
        ) as m4, patch(
            'well_architected_review_mcp_server_for_sagemaker.validators.operational_excellence.AwsHelper'
        ) as m5, patch(
            'well_architected_review_mcp_server_for_sagemaker.validators.sustainability.AwsHelper'
        ) as m6:
            for m in [m1, m2, m3, m4, m5, m6]:
                m.create_boto3_client.return_value = mock_client

            findings = run_all_validators('endpoint', info, [])
            pillars = {f['pillar'] for f in findings}
            assert 'Security' in pillars
            for f in findings:
                assert 'pillar' in f
                assert 'severity' in f
                assert 'resource' in f
                assert 'check' in f
                assert 'detail' in f
                assert 'recommendation' in f
