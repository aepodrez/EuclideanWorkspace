#!/bin/bash
set -e

EUCLIDEAN_DIR="$(pwd)/Euclidean"
mkdir -p "$EUCLIDEAN_DIR"
cd "$EUCLIDEAN_DIR"

repos=(
  "https://github.com/aepodrez/ExecutionModel.git"
  "https://github.com/aepodrez/AlphaModel.git"
  "https://github.com/aepodrez/DataIngressModel.git"
  "https://github.com/aepodrez/PortfolioConstructionModel.git"
  "https://github.com/aepodrez/EuclideanInfra.git"
  "https://github.com/aepodrez/UniverseModel.git"
)

for repo in "${repos[@]}"; do
  name=$(basename "$repo" .git)
  if [ -d "$name" ]; then
    echo "Skipping $name (already exists)"
  else
    echo "Cloning $name..."
    git clone "$repo"
  fi
done

echo "Done. All repos cloned to $EUCLIDEAN_DIR"
