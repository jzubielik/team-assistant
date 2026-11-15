// The agent's answers are Markdown. Model text is never trusted as HTML:
// markdown-it escapes raw HTML (html: false), links must be http or https, and images are off,
// so an answer cannot run script or make the browser load anything. The page inserts the
// result as HTML only because of these settings; tests pin each of them.
//
// Issue lists: the agent writes "- **[#<number>](<url>)** <title> `label`"
// (the number a link to the issue; a plain "**#<number>**" works too); such a list item gets
// class "issue" and its inline code class "label", which styles.css draws as a row with pills.

import MarkdownIt from "markdown-it";

const md = new MarkdownIt({ html: false, linkify: true, typographer: false });
md.disable("image");
md.validateLink = (url: string) => /^https?:\/\//i.test(url.trim());

const defaultLinkOpen =
  md.renderer.rules.link_open ?? ((tokens, idx, options, _env, self) => self.renderToken(tokens, idx, options));

md.renderer.rules.link_open = (tokens, idx, options, env, self) => {
  const token = tokens[idx]!;
  token.attrSet("target", "_blank");
  token.attrSet("rel", "noopener noreferrer");
  return defaultLinkOpen(tokens, idx, options, env, self);
};

const ISSUE_NUMBER = /^#\d+$/;

md.core.ruler.push("issue_rows", (state) => {
  const tokens = state.tokens;
  for (let i = 0; i < tokens.length; i++) {
    if (tokens[i]!.type !== "list_item_open") continue;
    // A list item holds paragraph_open, inline, paragraph_close (tight lists hide the
    // paragraph tags but keep the tokens).
    const inline = tokens[i + 2];
    const children = inline?.type === "inline" ? (inline.children ?? []) : [];
    // markdown-it 15 starts such a line with an empty text token before the bold; the number
    // may be a link inside the bold.
    const parts = children.filter((c) => !(c.type === "text" && c.content === ""));
    const [open, second, third] = parts;
    const number = second?.type === "link_open" ? third : second;
    if (open?.type !== "strong_open" || number?.type !== "text" || !ISSUE_NUMBER.test(number.content)) continue;
    tokens[i]!.attrJoin("class", "issue");
    for (const child of children) if (child.type === "code_inline") child.attrJoin("class", "label");
  }
});

export function renderMarkdown(text: string): string {
  return md.render(text);
}
