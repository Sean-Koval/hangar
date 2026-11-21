# Confluence pages a ticket links to

A Jira ticket's design docs, runbooks and decisions usually live in Confluence. hangar reads the pages a ticket links to and hands them to the agent as context files. It reads them on the control plane, with the Jira site's own token, as it reads the ticket. The sandbox gets no Atlassian credential and no Atlassian egress.

## Which pages

For a Jira-started job, hangar collects page links in this order, and reads each page once:

1. the ticket's remote links to Confluence;
2. page URLs in the description;
3. page URLs in comments by people linked to a hangar person.

A comment by anyone else is still in `jira-<key>.md`, but hangar doesn't read the pages it links to.

hangar reads a page only on the Jira site's own host, under `/wiki`. A remote link to another host is listed and never read, so the token never leaves its site. A short link (`/wiki/x/...`) is read when Confluence on that site redirects it to a page.

hangar reads a page only when the person who started the job can read it too: Confluence's permission check answers that first. So a link can't pull a restricted page into a job its starter couldn't open. What the starter can read may still reach the job's result, which hangar posts on the ticket.

Checking what another person can read may need Confluence's administrator permission on hangar's account. Without it, every link says "hangar's token can't check who may read Confluence pages" and no page is read.

Granting it makes hangar's Jira token a Confluence administrator too, a far more powerful account. Weigh that before you grant it; leaving the pages unread is the safe default (KAN-564).

## What the agent gets

Each page becomes `confluence-<id>-<slug>.md` in the job's context directory. The file starts with the page's id, space key and link. Everything the page's editors wrote follows between two markers that call it untrusted. That is the title, space name, last editor and the text as Markdown.

The file says when the page has child pages, but doesn't name them. Their titles come from hangar's token, which may see more than the starter.

The files are marked untrusted, as `jira-<key>.md` is: anyone who can edit a page wrote it. The agent reads them as reference, never as instructions. The team harness's `confluence-context` skill says how to cite a page and when to ask.

`jira-<key>.md` has a line for each Confluence link on this site and each remote link. The line names the file the page was read into, or says why it wasn't read.

## Limits

| `confluence` field | Meaning |
| --- | --- |
| `pages` | The most pages a job reads (5, at most 20). `0` reads none and only lists the links |
| `page_max_bytes` | Where a page's text is cut, with a note saying so (50,000; 2,000 to 500,000) |
| `search_spaces` | Space keys to search for the ticket's summary. Empty, the default, means no search |
| `search_results` | How many search results to read (3), within `pages` |

The fields go under `confluence` in `HANGAR_JIRA`. Links come first; search results fill what is left of `pages`, and their files say they were found by search.

A page that can't be read is a line in `jira-<key>.md`, never a job that doesn't start. hangar gives Confluence 15 seconds after the ticket's own context. A page still loading then is a line, and the others still come. The pages together also stay within what a job's context can hold.

## Not yet

- **Data Center.** On Data Center, links are listed, not read.
- **Links inside a page.** hangar reads one level: the pages the ticket links to.
- **Other sources.** Helpjuice and other knowledge bases can come later through the tracker plugin API.
