# Contributing

Thank you for your interest in sequentialist.

## Contributor License Agreement

sequentialist is offered under the GNU AGPL v3 and under commercial licenses
(see [COMMERCIAL.md](COMMERCIAL.md)). To keep that possible, Novikov
Laboratories LLC must hold the rights needed to license every part of the
code under both. A contribution can therefore be merged only after its
author has signed the Contributor License Agreement ([CLA.md](CLA.md),
Russian and English; a draft until it has been reviewed by a lawyer), sent
to andrei@hobukob.ru. Small fixes of typos in documentation are welcome
without it.

If you write code as part of your job, your employer may own it; in that
case the employer has to sign the CLA as well.

## Development

The package is being written; see [README.md](README.md).

- Every library runs on Python 3.8 and later and needs nothing but
  sequentialist-core and sequential-analysis (CI checks both, `vermin`
  checks the language level).
- Statistics belong in sequential-analysis. A library describes a task,
  handles its data, decides and reports; it does not compute tests.
- Every conclusion a library reports names its guarantee (`exact`,
  `asymptotic`, `model`, `approximate`, `none`), as the procedure of
  sequential-analysis states it.
- New source files start with the SPDX header used throughout the project.
