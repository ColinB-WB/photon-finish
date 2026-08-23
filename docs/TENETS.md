# Project Tenets

These tenets state the beliefs we use to make difficult product and engineering decisions.
They are intended to resolve tradeoffs, not merely describe desirable qualities.

They are presented as one numbered list so each belief is simple to find, remember, and
reference. Together they describe the product we are trying to make and the standards we use
while making it.

### 1. The photons are the authority

Use every trustworthy clue available—the image pixels, target identity, coordinates, filter,
exposure, and relevant catalog knowledge—but let measurements from the image decide what the
capture supports and how far it can safely be processed. Processing can reveal recorded faint
signal and mitigate noise, gradients, and imperfect observing conditions; it cannot recover
signal or detail that the capture did not contain.

When the evidence is insufficient or conflicting, preserve both the metadata and the measurement,
report the uncertainty, limit the disputed adjustment or abstain, and never invent astronomical
structure. A target name may help identify the intended object; it does not authorize
target-specific processing values or prove that the object was successfully captured.

**Use every trustworthy clue. Let the photons decide.**

### 2. Source data is sacred

The app never modifies or deletes original observation data. It duplicates data rather than
risking loss, even when duplication consumes additional storage.

The app may clean up only disposable working files that it created and can reproduce. Users
remain responsible for managing their source archives when storage capacity is constrained.

### 3. The finished photo is the product

The primary customer owns a Seestar S30 or S30 Pro and wants a noticeably better photo than the
device default without learning or performing photo editing. If producing a good result requires
an editing decision, the app should usually make that decision rather than hand it to the user.

### 4. Consistently very good beats occasionally best

The app approximates what an expert would do, but it does not claim to produce the one canonical
image—there is no single canonical astrophotograph. We prefer a robust, defensible result across
classes of images over a fragile method that occasionally produces the best possible result. We
optimize for classes of photographs, not individual trophy targets.

Success means producing, without editing, a trustworthy photo that is clearly better than the
device default and resembles a good expert treatment—even if a skilled person could spend more
time producing something better.

### 5. Reveal without destroying

The app must make the signal captured in the photograph visibly useful, not merely preserve it
somewhere in the pixel values. Preservation protects information that is difficult or impossible
to recover; it is not permission to produce an image that is technically faithful but effectively
empty.

An image that contains faint structure but leaves it invisible has failed the product goals in
*The finished photo is the product* and *Consistently very good beats occasionally best*. We
accept some visible noise when it is the honest cost of revealing real signal. We do not accept
clipping, crushed structure, bloated stars, fabricated detail, or other irreversible damage
merely to create a more dramatic image.

When these goals are in tension, reveal as much as the captured signal safely supports and stop
before the treatment becomes destructive.

### 6. Copy established practice; invent only the missing layer

Reuse proven astronomical methods and specialist tools for calibration, gradient removal, star
separation, detection, and stretching. The missing layer is the expert judgment required to run
those tools unattended: deciding which operation applies, when to use it, which bounded settings
to try, how to compare the results, and when to stop or report uncertainty.

For example, GraXpert already knows how to remove a background gradient, so we will not write our
own gradient-removal engine. We will determine when gradient removal is appropriate, recognize
when a frame-filling nebula makes the normal settings risky, try safer settings, and verify that
useful structure was not removed. Likewise, Siril performs stretches and StarNet removes stars;
the app decides when and how to use those capabilities.

This tenet intentionally accepts dependencies on specialist tools rather than producing weaker
built-in substitutes.

### 7. Trust requires bounded repeatability

The app should make the same explainable processing decisions when given the same input,
configuration, and measured evidence. The specialist tools it uses may produce small numerical or
rendering differences across operating systems, hardware, and CPU/GPU implementations; outputs are
not required to be byte-identical.

Results must instead be materially equivalent: they should express the same processing intent,
remain within validated quality tolerances, pass the same safety expectations, and avoid
meaningful visual regressions. The app records its version, tool and model versions, environment,
parameters, measurements, and decisions so differences can be understood and investigated. A
dependency may use AI internally, but its role in the pipeline must remain pinned, bounded, and
testable; we adopt a nondeterministic capability only when its variation can be controlled and its
outputs can still pass deterministic safety and quality gates.

We prefer bounded, regression-tested variation over uncontrolled behavior that occasionally
produces a better image. Byte-level identity is required only where explicitly appropriate, such
as pure internal decision logic or a same-environment test designed to isolate one change. Three
practical standards follow:

- **Internal policy and decisions:** deterministic.
- **External-tool pixels:** allowed to vary within validated tolerances.
- **User-visible quality and safety:** consistent across supported environments.

A step or gate that silently changes, skips, or degrades is a defect—not acceptable uncertainty.

### 8. Uncertainty is an honest outcome

When the app cannot confidently distinguish nebula from gradient, identify the subject, or
certify a preferred render, it should produce the safest useful result and explain the
uncertainty.

A qualified result is better than false confidence, fabricated precision, or silent failure. The
app should distinguish limitations in the source data from failures in its own processing
whenever it can do so honestly.
