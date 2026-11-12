# support-solutions-aws

A collection of curated solutions created by AWS Support. Each solution addresses a
specific customer problem that AWS Support engineers encounter in the field, and lives
in its own directory grouped by category.

Solutions are self-contained: every directory has a README covering what the solution
does, how to deploy it, and how to clean it up.

## Available solutions

### AI-ML

| Solution | Description |
|----------|-------------|
| [Bedrock Production Readiness Advisor](AI-ML/bedrock-production-readiness-advisor/) | Assesses Amazon Bedrock workloads for production readiness across security, cost, quota, guardrails, observability, and architecture. |
| [Guidance for AI-Powered GPU Capacity Planning on AWS](AI-ML/guidance-for-gpu-capacity-planning-on-aws/) | An Amazon Bedrock agent that assesses GPU capacity risk for inference and training on Amazon SageMaker AI and Amazon EC2, returning a deterministic confidence score, feasibility verdict, and capacity-mitigation recommendation from read-only AWS APIs. |
| [HibernateAI](AI-ML/hibernate-ai/) | A read-only Amazon CloudWatch custom widget that uses Amazon Bedrock to analyze usage across 15+ AWS services and recommend idle non-production resources to hibernate for cost savings — advisory only, no mutating actions. |
| [Well-Architected Review MCP Server for Amazon SageMaker](AI-ML/well-architected-review-mcp-server-for-sagemaker/) | Validates Amazon SageMaker workloads against the six AWS Well-Architected pillars using read-only APIs; returns prioritized findings with severities and recommendations. Runs locally in an IDE or hosted on Amazon Bedrock AgentCore. |


### Operations

| Solution | Description |
|----------|-------------|
| [Service Lifecycle Tracker on AWS](Operations/service-lifecycle-tracker/) | Automatically track AWS service deprecations, find the resources in your account that are affected, and manage remediation. |

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for what belongs here and
how to submit a solution. Please also review our [Code of Conduct](CODE_OF_CONDUCT.md).

## Disclaimer

Before using a solution, you should consider conducting your own independent assessment to
ensure that your use would comply with your own specific security and quality control
practices and standards, as well as the laws, rules, and regulations that govern you and
your content.

## Security

See [SECURITY.md](SECURITY.md) for how to report a security issue. Please do not create a
public GitHub issue.

## License

This project is licensed under the MIT-0 License. See [LICENSE](LICENSE) for details.
