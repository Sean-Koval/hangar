# Taking a repo's reviewer attention baseline

[Reviewer attention](metrics.md#reviewer-attention) per merged agent PR means little without a number from before. So record a baseline for a repo before hangar's first job there. Repo 1 needs one before hangar is used on it.

The baseline is the same measure on the repo's own merged PRs: review events per merged PR. It covers the four ISO weeks (UTC) before hangar starts in the repo. It leaves out PRs a bot opened, and reviews and comments by bots. The PR's author counts, as on agent PRs.

## Take it

Run this with the `gh` CLI, signed in with read access to the repo. Set the repo and the four weeks first.

```sh
REPO=owner/repo-1 SINCE=2026-10-05 UNTIL=2026-11-01   # four ISO weeks, UTC, before hangar's first job

count() {  # $1 PR number, $2 reviews|comments
  gh api --paginate "repos/$REPO/pulls/$1/$2" \
    --jq '.[] | select(.user.type != "Bot" and .state != "PENDING") | .id' | wc -l
}

gh pr list -R "$REPO" --state merged --limit 1000 --search "merged:$SINCE..$UNTIL" \
  --json number,author --jq '.[] | select(.author.is_bot | not) | .number' |
while read -r n; do
  echo "$n $(count "$n" reviews) $(count "$n" comments)"
done | tee baseline.txt |
awk '{ e += $2 + $3 } END { printf "%d PRs, %d review events, %.2f per merged PR\n", NR, e, NR ? e / NR : 0 }'
```

`baseline.txt` keeps one line per PR: its number, its reviews and its review comments.

## Record it

Put the summary line, the dates and `baseline.txt` on the repo's onboarding ticket in Jira. Then compare it with `reviewer_attention.events_per_merged_pr` from `hangar metrics weekly --repo owner/repo-1`.

The two differ in one way. The baseline counts humans' PRs, and hangar counts agent PRs. Change both if the definition changes.
