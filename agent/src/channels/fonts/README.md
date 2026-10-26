# Report font

`VibeReportCJK-Regular.ttf` is a static weight-400 instance of Noto Sans SC,
renamed to Vibe Report CJK. It retains all 30,890 Unicode mappings from the
source font, including simplified/traditional Chinese, Latin text and numbers.
Both PDF renderers use this bundled font; report generation needs no font
download or locally installed CJK font. ReportLab embeds only the glyphs used
by each document. Its simple fallback uses this regular face for bold/italic
spans as well, preserving readable Chinese text in every span.

Source: [notofonts/noto-cjk](https://github.com/notofonts/noto-cjk/blob/f8d157532fbfaeda587e826d4cd5b21a49186f7c/Sans/Variable/TTF/Subset/NotoSansSC-VF.ttf)
at commit `f8d157532fbfaeda587e826d4cd5b21a49186f7c`.
Copyright and redistribution terms are preserved in `OFL.txt` and the font's
name table (SIL Open Font License 1.1).

Source SHA-256: `d68bafcb48a2707749396aa12bbbd833cb70401f3a9a689fd2902c7e0d295964`.
Bundled SHA-256: `ddbc595de650a5278fe628966edf24a37d35d23e148a9a9e9d7f13327d51030a`.

To regenerate, use FontTools' `instantiateVariableFont(font, {"wght": 400},
inplace=True)`, then rename name IDs 1/16 to `Vibe Report CJK`, 2/17 to
`Regular`, 3 to `VibeReportCJK-Regular-1.0`, 4 to `Vibe Report CJK Regular`,
and 6 to `VibeReportCJK-Regular`. Preserve all other metadata and glyphs.
FontTools is needed only when updating this asset, not at runtime.
