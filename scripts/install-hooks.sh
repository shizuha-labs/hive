#!/bin/bash
# One-time per checkout: activate the committed .githooks (pre-push CI gate).
cd "$(git rev-parse --show-toplevel)"
git config core.hooksPath .githooks
echo "hooks activated: $(git config core.hooksPath) (pre-push gates main/master behind the local CI suite)"
