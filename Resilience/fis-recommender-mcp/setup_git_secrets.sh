#!/usr/bin/env bash
# Setup git-secrets for this repository
# Usage: ./setup_git_secrets.sh

set -e

if ! command -v git-secrets &> /dev/null; then
    echo "git-secrets not found. Installing..."
    if command -v brew &> /dev/null; then
        brew install git-secrets
    elif command -v apt-get &> /dev/null; then
        git clone https://github.com/awslabs/git-secrets.git /tmp/git-secrets
        cd /tmp/git-secrets && sudo make install && cd - && rm -rf /tmp/git-secrets
    else
        echo "Please install git-secrets: https://github.com/awslabs/git-secrets"
        exit 1
    fi
fi

# Install hooks
git secrets --install --force

# Register AWS patterns (AKID, secret keys, account IDs)
git secrets --register-aws

# Additional patterns
git secrets --add 'password\s*=\s*.+' 2>/dev/null || true
git secrets --add 'secret\s*=\s*.+' 2>/dev/null || true
git secrets --add "PRIVATE"'.KEY' 2>/dev/null || true

# Allowed patterns (prevent false positives on example/placeholder values)
git config --add secrets.allowed 'arn:aws:iam::123456789012:role/'
git config --add secrets.allowed 'EXAMPLE'
git config --add secrets.allowed 'placeholder'

echo "✅ git-secrets configured. Secrets will be checked on every commit."
echo "   Run 'git secrets --scan' to scan existing files."
