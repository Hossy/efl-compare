# Fonts

Self-hosted rather than linked from `fonts.googleapis.com`, so the site doesn't
hand every visitor's IP to a third party to render a page that otherwise phones
nobody — and so it still works offline. Latin subset only.

| Family | Weights | Designer | License |
|---|---|---|---|
| [Sora](https://fonts.google.com/specimen/Sora) | 400, 600, 700, 800 | Jonny Pinhorn / Indian Type Foundry | [SIL Open Font License 1.1](https://openfontlicense.org/) |
| [Karla](https://fonts.google.com/specimen/Karla) | 400, 500, 700 | Jonny Pinhorn | [SIL Open Font License 1.1](https://openfontlicense.org/) |

Both are OFL-1.1, which permits redistribution — including bundled with software
— provided the fonts aren't sold on their own and the licence travels with them.
That's compatible with this project's GPLv3: the OFL covers these files, the GPL
covers the code around them.

The `@font-face` rules live in [`../theme.css`](../theme.css). To add a weight,
fetch the `woff2` from the Google Fonts CSS API and drop it here as
`<family>-<weight>.woff2` — don't add a `<link>` to a font CDN.
