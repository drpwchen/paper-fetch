# Security

Please report vulnerabilities privately through GitHub: open the **Security** tab of this repository and choose **Report a vulnerability** (private vulnerability reporting). Please do not open a public issue for security problems.

This tool handles library credentials and publisher API keys and drives a browser session. In scope: anything that could leak those secrets (into logs, the repo, or the `--json` output), or that writes them to disk in plain text. Out of scope: publisher bot walls or paywalls, and misuse of the tool against your institution's terms (see [DISCLAIMER.md](DISCLAIMER.md)).

這個工具會碰到圖書館帳密、出版社 API key 與瀏覽器 session。發現可能洩漏這些資料的漏洞，請從本 repo 的 **Security** 分頁選 **Report a vulnerability** 私下通報，不要開公開 issue。
