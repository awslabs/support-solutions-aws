# Architecture steps

> **Disclaimer:** This is sample code, for non-production usage. You should work with your security and legal teams to meet your organizational security, regulatory, and compliance requirements before deployment.

The reference architecture is `guidance-architecture.pptx` (built on the official Guidance
reference-architecture template) with a rendered copy in `assets/images/guidance-architecture.png`.
The `architecture-draft.dac.yaml` file is an AWS Diagram-as-Code draft used while iterating on the
layout.

## Steps as they appear on the slide

These match the numbered callouts and become the "How it works" section of the Guidance page.
The style guide asks for 10-20 words per step, in Arial 10 pt.

1. An ML infrastructure engineer sends a natural-language capacity question to Amazon API Gateway
   (or directly to an AWS Lambda function URL that requires AWS Identity and Access Management (IAM)
   authentication, which bypasses Amazon API Gateway).
2. On the API Gateway path, AWS WAF screens the request, and Amazon API Gateway authenticates it and
   invokes the agent AWS Lambda function.
3. The function calls Amazon Bedrock, a fully managed service for building generative AI
   applications, using the Converse API; the model returns tool-use requests that the function
   executes, feeding results back in a loop.
4. The function runs read-only tools against Amazon EC2, the AWS Price List API, Service Quotas, AWS
   CloudTrail, Amazon CloudWatch, and Amazon SageMaker AI for endpoint and training-job GPU usage.
5. The function caches tool results in an Amazon DynamoDB table with a short time to live, so repeat
   questions avoid duplicate API calls.
6. For a cross-account query, the function first assumes a read-only IAM role in the customer
   account through AWS STS, before the Step 4 tool calls run there.
7. The function computes a deterministic confidence score and returns a structured response (the
   risk score, supporting evidence, and recommended capacity mitigations) through Amazon API
   Gateway.
8. On an unrecoverable error, the function writes a failure record to an Amazon SQS dead-letter
   queue; an Amazon CloudWatch alarm notifies an Amazon SNS topic that emails the operator.

## Longer descriptions (for the README overview)

1. An ML infrastructure engineer sends a natural-language capacity question to the Amazon API Gateway REST
   endpoint, or to the AWS Lambda function URL for queries that run longer than 29 seconds. The function URL requires
   IAM authentication (`AuthType: AWS_IAM`) and is not fronted by AWS WAF.
2. AWS WAF screens the request for common web exploits and rate abuse. Amazon API Gateway
   authenticates it (AWS Identity and Access Management (IAM) or an API key) and invokes the AWS
   Lambda function that runs the agent.
3. AWS Lambda calls Amazon Bedrock through the Converse API, passing the read-only tool definitions.
   Amazon Bedrock returns the tools to call. Steps 3 and 4 repeat in a loop until the model has
   enough signal or the agent reaches its iteration limit. The model is configurable; the default is
   Anthropic Claude Haiku 4.5.
4. AWS Lambda runs the requested read-only tools against public AWS APIs: Amazon Elastic Compute
   Cloud (Amazon EC2) for instance availability, instance types, Spot placement scores, capacity
   reservations, and Capacity Blocks for ML; the AWS Price List API; Service Quotas; AWS
   CloudTrail; Amazon CloudWatch; and Amazon SageMaker AI. Independent tools run in parallel.
5. AWS Lambda caches tool results in an Amazon DynamoDB table with a short, configurable time to live
   (`CACHE_TTL_SECONDS`), so repeated questions avoid duplicate AWS API calls and reduce cost.
6. For a cross-account query, AWS Lambda assumes a read-only AWS Identity and Access Management (IAM)
   role in the customer account through AWS Security Token Service (AWS STS), passing an external ID.
   In single-account mode, the function queries its own account and skips this step.
7. AWS Lambda computes a deterministic confidence score from the raw tool outputs in
   `src/confidence.py`, assembles the structured decision package (verdict, signal table, cost
   projection, alternatives, and recommended capacity mitigations), and returns it through Amazon API Gateway.
8. AWS Lambda publishes invocation logs, token usage, and tool-latency metrics to Amazon CloudWatch,
   which backs the operational dashboard and alarms. A failed invocation goes to an Amazon Simple
   Queue Service (Amazon SQS) dead-letter queue, and a CloudWatch alarm on the queue depth notifies
   an encrypted Amazon Simple Notification Service (Amazon SNS) topic.
