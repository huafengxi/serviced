#!/bin/sh
# post-receive hook for dev bare mirrors (~/git/<repo>.git).
# Installed by `make git-mirror.hooks` (symlinked from ~/git/post-receive-mirror.sh).
#
# For every branch updated by a push into the mirror, fast-forward push it
# back to GitHub (git@github.com:huafengxi/<repo>.git). Push-back is strictly
# ff-only: a diverged (non-ff) branch is REJECTED by git push and we only
# WARN (hook output + ~/git/sync.log) — never force-push.
# Branch deletions are not propagated to GitHub (manual decision, see log).

REPO="$(basename "$(pwd)")"
REPO="${REPO%.git}"
GITHUB="git@github.com:huafengxi/$REPO.git"
LOG="$HOME/git/sync.log"
ZERO="0000000000000000000000000000000000000000"

ts() { date '+%Y-%m-%d %H:%M:%S'; }

while read -r old new ref; do
    case "$ref" in
        refs/heads/*) ;;
        *) continue ;;   # tags etc: not pushed back
    esac
    branch="${ref#refs/heads/}"
    if [ "$new" = "$ZERO" ]; then
        echo "$(ts) [$REPO] branch $branch deleted in mirror; NOT propagating delete to GitHub" >> "$LOG"
        continue
    fi
    # </dev/null: ssh inside a hook must not consume the hook's stdin
    out="$(git push "$GITHUB" "$branch" </dev/null 2>&1)"
    rc=$?
    if [ $rc -eq 0 ]; then
        echo "$(ts) [$REPO] push-back ok: $branch -> github" >> "$LOG"
        echo "mirror-hook: pushed branch '$branch' to GitHub (ff)"
    else
        {
            echo "$(ts) [$REPO] WARN push-back FAILED for $branch (non-ff diverged, or error); NOT force-pushing"
            echo "$out" | sed 's/^/    /'
        } >> "$LOG"
        echo "mirror-hook: WARN push-back of '$branch' to GitHub FAILED (diverged?); NOT force-pushed, see dev:~/git/sync.log" >&2
    fi
done
exit 0
