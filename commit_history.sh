#!/usr/bin/env bash
# Replays the build as a sequence of logical commits.
# Run once, from the project root, after setting your git identity.
set -e

git add .gitignore README.md requirements.txt
git commit -m "Initialise project scaffold"

git add DECISIONS.md
git commit -m "Record design decisions: language, money representation, fee model"

git add src/generate_data.py
git commit -m "Add synthetic data generator for ledger, gateway and bank records"

git add data/ledger.csv data/gateway.csv data/bank.csv data/ground_truth.csv
git commit -m "Add generated sample batch and ground-truth answer key"

git add NOTES.md
git commit -m "Log two data-integrity bugs found during generator verification"

git add src/core.py
git commit -m "Extract money, fee and calendar logic into shared core module"

git add data/settlements.csv
git commit -m "Emit settlement report linking gateway payments to bank credits"

git add src/matcher.py
git commit -m "Add tiered reconciliation engine with tier-tagged resolutions"

git add src/narration.py
git commit -m "Add tier 3 narration parsing with deterministic verification gate"

git add src/tools.py
git commit -m "Add deterministic investigation tools with model-facing schema"

git add src/agent.py
git commit -m "Add bounded exception resolution agent with enforced guards"

git add -A
git commit -m "Add requirements and repository setup" || true

echo
echo "Commits created:"
git log --oneline
