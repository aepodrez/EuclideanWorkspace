#!/bin/bash
set -e

echo "=== Setting up Git authentication ==="
# Configure git to use SSH
git config --global url."git@github.com:".insteadOf "https://github.com/"

# Fix SSH key permissions if they exist
if [ -d ~/.ssh ]; then
  chmod 700 ~/.ssh
  if [ -f ~/.ssh/id_rsa ]; then
    chmod 600 ~/.ssh/id_rsa
  fi
  if [ -f ~/.ssh/id_ed25519 ]; then
    chmod 600 ~/.ssh/id_ed25519
  fi

  # Add GitHub to known_hosts to avoid prompts
  ssh-keyscan -H github.com >> ~/.ssh/known_hosts 2>/dev/null || true

  # Fix macOS-specific SSH config options not supported on Linux (e.g. UseKeychain)
  if grep -qi "usekeychain" ~/.ssh/config 2>/dev/null; then
    LINUX_SSH_CONFIG="/home/vscode/.config/ssh/config"
    mkdir -p "$(dirname "$LINUX_SSH_CONFIG")"
    grep -iv "usekeychain" ~/.ssh/config > "$LINUX_SSH_CONFIG"
    chmod 600 "$LINUX_SSH_CONFIG"
    export GIT_SSH_COMMAND="ssh -F $LINUX_SSH_CONFIG"
    echo "export GIT_SSH_COMMAND=\"ssh -F $LINUX_SSH_CONFIG\"" >> ~/.bashrc
    echo "Fixed SSH config: removed macOS-only UseKeychain option"
  fi
fi

# Configure git user if not already set
if ! git config --global user.name >/dev/null 2>&1; then
  git config --global user.name "Dev Container User"
  git config --global user.email "dev@container.local"
fi

echo "=== Installing AWS CLI ==="
ARCH=$(uname -m)
if [ "$ARCH" = "aarch64" ]; then
  AWS_URL="https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip"
else
  AWS_URL="https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip"
fi
curl -s "$AWS_URL" -o /tmp/awscliv2.zip
unzip -q /tmp/awscliv2.zip -d /tmp
sudo /tmp/aws/install --update 2>/dev/null || sudo /tmp/aws/install
rm -rf /tmp/aws /tmp/awscliv2.zip
echo "✓ AWS CLI $(aws --version 2>&1 | head -1)"

echo "=== Installing Claude Code and Codex ==="
npm install -g @anthropic-ai/claude-code codex 2>/dev/null || echo "npm install skipped (npm may not be available)"

echo "=== Cloning Euclidean repositories ==="
EUCLIDEAN_DIR="$(pwd)/Euclidean"
mkdir -p "$EUCLIDEAN_DIR"
cd "$EUCLIDEAN_DIR"

repos=(
  "git@github.com:aepodrez/ExecutionModel.git"
  "git@github.com:aepodrez/AlphaModel.git"
  "git@github.com:aepodrez/DataIngressModel.git"
  "git@github.com:aepodrez/PortfolioConstructionModel.git"
  "git@github.com:aepodrez/EuclideanInfra.git"
  "git@github.com:aepodrez/UniverseModel.git"
)

for repo in "${repos[@]}"; do
  name=$(basename "$repo" .git)
  if [ -d "$name" ]; then
    echo "✓ Skipping $name (already exists)"
  else
    echo "Cloning $name..."
    if git clone "$repo" 2>&1; then
      echo "✓ Cloned $name"
    else
      echo "✗ Failed to clone $name"
      echo "  SSH keys may not be available. To fix:"
      echo "    - Ensure ~/.ssh/id_rsa or ~/.ssh/id_ed25519 exists on your host"
      echo "    - Or run: gh auth login"
      echo "    - Or configure: git config --global user.password '...'"
    fi
  fi
done

echo ""
echo "=== Setup complete ==="
echo "Repositories are in: $EUCLIDEAN_DIR"
