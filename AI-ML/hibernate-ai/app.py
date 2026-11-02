#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks
from stacks.ai_stack import HibernateAIStack

app = cdk.App()
stack = HibernateAIStack(app, "HibernateAI", env=cdk.Environment(region="us-east-1"))
cdk.Tags.of(stack).add("auto-delete", "no")

# cdk-nag: run AWS Solutions security/best-practice checks at synth time.
# Surfaces IAM, encryption, and resiliency findings in-code. Suppressions for
# intentionally-accepted findings live in stacks/__init__.py (NagSuppressions),
# each with a written reason.
cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))

app.synth()
