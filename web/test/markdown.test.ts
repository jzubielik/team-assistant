import { describe, expect, it } from "vitest";
import { renderMarkdown } from "../src/markdown";

describe("renderMarkdown", () => {
  it("keeps a script tag in model text as text", () => {
    const html = renderMarkdown("<script>alert(1)</script>");
    expect(html).not.toContain("<script");
    expect(html).toContain("&lt;script&gt;");
  });

  it("keeps an image tag with an event handler as text", () => {
    const html = renderMarkdown("<img src=x onerror=alert(1)>");
    expect(html).not.toContain("<img");
  });

  it("makes no link of a javascript: or data: address", () => {
    expect(renderMarkdown("[x](javascript:alert(1))")).not.toContain("<a");
    expect(renderMarkdown("[x](data:text/html,hi)")).not.toContain("<a");
  });

  it("makes no link to a host outside the list", () => {
    expect(renderMarkdown("[x](https://example.com/?q=secret)")).not.toContain("<a");
  });

  it("makes no link to a host that only starts with github.com", () => {
    expect(renderMarkdown("[x](https://github.com.example.com/)")).not.toContain("<a");
  });

  it("makes no link of an http address, even to github.com", () => {
    expect(renderMarkdown("[x](http://github.com/aws/aws-cdk)")).not.toContain("<a");
  });

  it("makes no link of a bare address outside the list", () => {
    expect(renderMarkdown("see example.com/x?q=secret")).not.toContain("<a");
  });

  it("loads no image, not even from https", () => {
    expect(renderMarkdown("![logo](https://example.com/logo.png)")).not.toContain("<img");
  });

  it("opens https links in a new tab without the opener or a referrer", () => {
    const html = renderMarkdown("[issues](https://github.com/aws/aws-cdk/issues)");
    expect(html).toContain('href="https://github.com/aws/aws-cdk/issues"');
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noopener noreferrer"');
  });

  it("draws an issue line as a row with label pills", () => {
    const html = renderMarkdown("- **#418** Memory drops the last event `bug`\n- plain item");
    expect(html).toContain('<li class="issue">');
    expect(html).toContain('<code class="label">bug</code>');
    expect(html).toContain("<li>plain item</li>");
  });

  it("makes the issue number a link to the issue, opened in a new tab", () => {
    const html = renderMarkdown("- **[#741](https://github.com/aws/x/issues/741)** Warmup hooks `enhancement`");
    expect(html).toContain('<li class="issue">');
    expect(html).toContain('<a href="https://github.com/aws/x/issues/741" target="_blank" rel="noopener noreferrer">#741</a>');
    expect(html).toContain('<code class="label">enhancement</code>');
  });

  it("does not take a bold word that is not an issue number for an issue", () => {
    expect(renderMarkdown("- **Note** this `x`")).not.toContain("issue");
  });

  it("keeps inline code outside issue rows plain", () => {
    expect(renderMarkdown("Run `make check`.")).toContain("<code>make check</code>");
  });
});
