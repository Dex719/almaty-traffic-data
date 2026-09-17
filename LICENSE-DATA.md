# Data licence: Creative Commons Attribution 4.0 International (CC BY 4.0)

This licence covers the **dataset** published by this project:

- everything under `data/` in the git repository (scores, event registries,
  road geometries and their registry versions, historical files);
- every asset of the GitHub Releases tagged `data-YYYY-MM` (daily archives
  `traffic-YYYY-MM-DD.tar.xz`) and of the internal prerelease `staging`.

The collector source code (`collector/`, `tests/`, `deploy/`, `.github/`) is
licensed separately under the MIT License, see `LICENSE`.

## Terms

You are free to **share** (copy and redistribute in any medium or format) and
**adapt** (remix, transform, build upon) the data for any purpose, including
commercially, under the following condition:

- **Attribution.** Give appropriate credit, provide a link to the licence and
  indicate if changes were made. Suggested form:

  > Almaty traffic data, https://github.com/Dex719/almaty-traffic-data,
  > licensed under CC BY 4.0.

Full legal text: https://creativecommons.org/licenses/by/4.0/legalcode

## Notices

- **Upstream sources.** The values were observed through public endpoints of
  Yandex and 2GIS. This licence applies to the compilation, structure and
  derived files produced by this project; it does not grant any rights in the
  services or trademarks of those providers, and their own terms continue to
  govern direct use of their services. Check them before republishing raw
  provider content at scale.
- **Personal data.** Driver comments inside event records may contain personal
  data. Reusers are responsible for complying with applicable privacy law and
  for removing such content on request.
- **No warranty.** The data is provided "as is", without warranty of
  completeness, accuracy or fitness for a particular purpose. Gaps, partial
  observations and provider outages are recorded rather than hidden; read
  `README.md` for the semantics of every field before drawing conclusions.
