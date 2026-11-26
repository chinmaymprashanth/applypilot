"""Outreach: for each job you apply to, two people to contact and a drafted message. You always click Send.

- contacts.py   the records (stored in data/jobagent.db with the jobs), the follow-up schedule, the weekly funnel
- find.py       who to contact (public, non-LinkedIn sources), LinkedIn search links, email guesses, Hunter checks
- draft.py      Claude's drafts, fact-checked against your master resume like every other draft
- mail.py       Gmail: create drafts (never send), notice what you sent and who replied
- assistant.py  the step after "Submitted? y" and the daily follow-up pass

LinkedIn is never automated: jobagent only builds a search link, opens it in your own Chrome and puts the
note on your clipboard.
"""
