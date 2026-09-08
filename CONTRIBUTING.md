# Contributing to aws-support-solutions

Thank you for your interest in contributing. This repository is a curated collection of
solutions created by AWS Support to help customers diagnose, troubleshoot, and operate
their AWS environments.

## What belongs here

A good submission is:

- **Useful** — it addresses a recurring problem, not a one-off script for a single account.
- **Impactful** — it saves meaningful time or reduces risk for the people who run it.
- **Safe** — nothing destructive by default, credentials and API calls handled
  responsibly, and it fails safely.
- **Genuinely helpful to customers** — written so that someone outside your team can pick
  it up, with enough context on what it does and why.

## Contributing a new solution

1. Fork this repository and create a directory for your solution under the category folder
   that fits it best (for example `Resilience/`). If no existing category fits, create one.
2. Add a `README.md` in your solution's directory covering what it does, how to deploy and
   use it, the IAM permissions it requires, any prerequisites, and how to clean up.
3. Submit source code only. Do not include pre-built or compiled artifacts such as `.zip`,
   `.exe`, or `.jar` files — users need to be able to read what they are running.
4. Add the license header to each source file:

   ```
   # Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
   # SPDX-License-Identifier: MIT-0
   ```

5. Add your solution to the table in the top-level [README.md](README.md).
6. Open a pull request describing what the solution does and how you validated it.

Before opening a pull request, check that your submission contains no credentials, account
IDs, internal hostnames, personal email addresses, or other sensitive data — in the files
*or* in your commit history. Everything in a pull request is publicly visible.

## Fixing or improving an existing solution

Direct your pull request at that solution's directory and keep the changes limited to it.
Update its `README.md` if the way it is invoked or its requirements change.

## Testing

There is no repository-wide test suite. If a solution has its own tests, run them. If it
does not, explain your validation approach in the pull request — what you ran it against
and what you checked.

## Review

A maintainer will usually review your pull request within a few business days. If you have
not heard anything after about two weeks, feel free to add a comment on the pull request.

## Reporting issues or requesting features

Open a GitHub issue. Include the solution name, steps to reproduce, and expected versus
actual behavior, so we do not have to come back with follow-up questions.

## Code of conduct

See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## Security

Do not report security issues through GitHub issues or pull requests. See
[SECURITY.md](SECURITY.md).

## License

Contributions are licensed under the MIT-0 License. See [LICENSE](LICENSE).
