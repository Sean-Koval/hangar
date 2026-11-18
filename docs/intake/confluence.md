# Confluence pages a ticket links to

A Jira ticket's design docs, runbooks and decisions usually live in Confluence. hangar reads the pages a ticket links to and hands them to the agent as context files. It reads them on the control plane, with the Jira site's own token, as it reads the ticket. The sandbox gets no Atlassian credential and no Atlassian egress.

## Which pages

For a Jira-started job, hangar collects page links in this order, and reads each page once:

1. the ticket's remote links to Confluence;
2. page URLs in the description;
3. page URLs in comments by people linked to a hangar person.

A comment by anyone else is still in `jira-<key>.md`, but hangar doesn't read the pages it links to.

hangar reads a page only on the Jira site's own host, under `/wiki`. A link to another host is listed and never read, so the token never leaves its site. A short link (`/wiki/x/...`) is read when Confluence on that site redirects it to a page.

## What the agent gets

Each page becomes `confluence-<id>-<slug>.md` in the job's context directory. The file starts with the title, the space, the last editor and the date. The page's text follows as Markdown, between markers that call it untrusted. Child pages are listed by title, not read.

The files are marked untrusted, as `jira-<key>.md` is: anyone who can edit a page wrote it. The agent reads them as reference, never as instructions. The team harness's `confluence-context` skill says how to cite a page and when to ask.

`jira-<key>.md` lists every Confluence link. Each line names the file it was read into, or says why it wasn't read.

## Limits

| `confluence` field | Meaning |
| --- | --- |
| `pages` | The most pages a job reads (5). `0` reads none and only lists the links |
| `page_max_bytes` | Where a page's text is cut, with a note saying so (50,000) |
| `search_spaces` | Space keys to search for the ticket's summary. Empty, the default, means no search |
| `search_results` | How many search results to read (3), within `pages` |

The fields go under `confluence` in `HANGAR_JIRA`. Links come first; search results fill what is left of `pages`, and their files say they were found by search.

A page the token can't read is a line in `jira-<key>.md`, not a failed job. hangar gives Confluence 15 seconds after the ticket's own context. Past that, the job starts without the pages and says so.

## Not yet

- **Data Center.** On Data Center, links are listed, not read.
- **Links inside a page.** hangar reads one level: the pages the ticket links to.
- **Other sources.** Helpjuice and other knowledge bases can come later through the tracker plugin API.
