# Frequently asked questions

## Do I need to know astrophotography editing?

No. The default workflow is designed for a Seestar owner who wants a finished
photo without learning GraXpert, Siril, StarNet2, and metadata tools first.
Those applications remain available if you later want to learn a manual
workflow.

## Why not process the Seestar JPG?

The stacked FITS retains much more of the measured brightness and color data.
The stock JPG is already stretched and compressed, which limits what later
processing can recover.

## Why must I install four other applications?

They are excellent specialist tools. Reimplementing gradient removal, image
development, star separation, and metadata handling would be wasteful and
unlikely to improve on their work. This project coordinates them into one
measured workflow.

## Will it change or delete my Seestar files?

No. The source library is treated as read-only. Generated and temporary files
belong under the separate output folder you choose.

## Are my images uploaded?

No. Image processing is local. An optional astronomy-catalog request may send
only sky coordinates; see [Privacy and network use](privacy-and-network.md).

## Does it work offline?

Image processing and the bundled Messier and Caldwell catalog work offline. An
optional SIMBAD query can add catalog evidence for other fields, but a run does
not depend on it.

## Does the object name determine the edit?

No. The frame's filter and measurements determine processing. Catalog identity
is bounded evidence for classification and never overrides measurements,
filter rules, quality checks, or pixel operations.

## Can it remove satellite trails?

It detects trails and attempts a conservative repair when supported by the
evidence. Difficult trails can leave remnants; the app does not conceal
uncertainty by inventing replacement structure.

## Can it combine several nights?

Not in v1. When several qualifying sessions exist for a target, the latest is
used. Multi-session integration is future work.

## Which style should I choose?

Start with Showcase. Try Natural for a more restrained look, Reference for an
observing record with annotation, or Print when preparing a paper output.

## Why did I get several images?

Suitable nebulae and galaxies benefit from full-star, reduced-star, and starless
versions. Galaxies may also benefit from a close-up. Clusters keep their stars
because the stars are the subject.

## Can I change one result?

Yes. After rendering one target, open **Advanced: manually adjust this image**.
See [Fine-tune one image](manual/fine-tuning.md).

## Is this affiliated with ZWO or the helper tools?

No. It is an independent hobby project, made possible by the Seestar ecosystem
and the open-source applications it orchestrates.
