# What the vendors allow

What Anthropic's and OpenAI's terms say about sharing a login and about automated use, and what hangar does about
it. Checked on 2026-10-14 against the vendors' own pages. This is a summary, not legal advice; read the terms that
apply to your plan.

## Anthropic

Free, Pro and Max logins are under the [Consumer Terms](https://www.anthropic.com/legal/consumer-terms). They say: "You
may not share your Account login information, Anthropic API key, or Account credentials with anyone else". They also
say: "You also may not make your Account available to anyone else."

Claude Code's [legal and compliance page](https://code.claude.com/docs/en/legal-and-compliance) adds that Pro and Max
usage limits "assume ordinary, individual usage". Configuring an API key "for use by the customer's own authorized
users" is fine when the usage is billed to the key's owner.

The same page says Anthropic doesn't permit developers "to route requests through Free, Pro, or Max plan credentials
on behalf of their users".

It covers Claude Code running "in hosted sandboxes or other agent infrastructure". There, "each end user must
authenticate with their own Anthropic API key, Claude subscription plan credentials, or 3P inference provider
credential". A team subscription login used for everyone's jobs is exactly that kind of routing.

## OpenAI

The [Terms of Use](https://openai.com/policies/terms-of-use/) say: "You may not share your account credentials or make
your account available to anyone else". The [Codex authentication docs](https://developers.openai.com/codex/auth) say
to "Use API key authentication for programmatic Codex CLI workflows, such as CI/CD jobs". They say to treat
`~/.codex/auth.json` like a password.

In a ChatGPT Enterprise workspace, an admin can also let members create Codex access tokens "for trusted scripts,
schedulers, and private CI runners". A member creates one, so store it as theirs, like a login.

## So in hangar

**A subscription login (a Claude OAuth token, a ChatGPT `auth.json`, a Google sign-in) is one person's own**, used only
in their sandboxes. **Team credentials are API keys**, such as an Anthropic Console key and an OpenAI project key
billed to your organization. Other credentials the vendor's terms allow for automation count too.

hangar refuses a subscription login as a team credential outright, even one for a dedicated account, since everyone's
jobs would route through it.

Say one is stored as the team's anyway, by an older version or around the API. Then
`hangar doctor` fails until it is someone's own or removed. How hangar uses the ones a person stores is on
[subscription logins](subscription-logins.md).
