"""Command-line entry point. Thin; the operator (Claude Code) drives the
adaptive choices and can pass overrides programmatically via run.run_target."""

from __future__ import annotations

import argparse
import json
import sys


def _print_run(s: dict) -> None:
    print(
        f"\n{s['target']} [{s['profile']}] — "
        f"{s['inspection']['classification']['category']}, "
        f"{s['inspection']['meta']['total_integration_min']} min, "
        f"{s['inspection']['meta']['filter']}"
    )
    cr = s.get("notes", {}).get("crop")
    if cr:
        print(f"  {cr['summary']}")
        if cr.get("warning"):
            print(f"  WARNING: {cr['warning']}")
    bp = s.get("notes", {}).get("blackpoint")
    if bp:
        print(
            f"  black point: {bp['blackpoint_on_linear']} (linear master); "
            f"raw-vs-linear median delta {bp['raw_minus_linear_median']}"
        )
    from .qa import solver_warnings

    for w in solver_warnings(s.get("notes", {}).get("stretch_solve")):
        print(f"  WARNING: {w}")
    lp = s.get("notes", {}).get("local_prior") or {}
    if lp.get("outcome") == "contradicted" and lp.get("warning"):
        print(f"  WARNING: {lp['warning']}")
    for o in s["outputs"]:
        print(f"  {o['variant']:16s} {o.get('jpg')}")


def _print_status_failure(s: dict) -> None:
    """I1 (final review): a status-failed target (`run_target` completed without
    raising, e.g. an M2 fail-closed QA gate, but its result `status` is neither
    "pass" nor "source_limited") must not print identically to a success -- name
    the target, its status, and the hero-contract reasons explicitly."""
    status = s.get("status", "unknown")
    reasons = (s.get("notes") or {}).get("hero_contract", {}).get("reasons") or []
    print(f"  FAILED: {s['target']} — status: {status}")
    for r in reasons:
        print(f"    - {r}")
    # Task 13: an uncertified render's hero deliverable is still on disk under its
    # uncertified name (the publication namespace, spec rev 6) -- name it, so a
    # failure line never leaves the operator hunting for what was actually written.
    pub = s.get("published")
    if isinstance(pub, dict) and pub.get("certified") is False and pub.get("hero_base") is not None:
        print(f"    preserved for inspection as {pub['hero_base']}.jpg")


def _print_replay_report(s: dict) -> None:
    rep = (s.get("notes") or {}).get("recipe_replay")
    if not rep:
        return
    print("\n  recipe replay — what changed vs the prior render:")
    for w in rep.get("warnings", []):
        print(f"    warn: {w}")
    print(f"    {'adjustment':20s} {'prior':>10s} {'now':>10s} {'source':>11s}  changed")
    for c in rep.get("changes", []):
        print(
            f"    {c['adjustment']:20s} {str(c['prior']):>10s} {str(c['now']):>10s} "
            f"{c['source']:>11s}  {'yes' if c['changed'] else 'no'}"
        )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="seestar", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("inspect", help="Measure + classify a FITS; print JSON")
    sp.add_argument("fits")

    def _add_render_opts(sp):
        sp.add_argument("--profile", default="showcase", choices=["showcase", "natural", "documentation", "print"])
        sp.add_argument("--debug", action="store_true", help="Save a snapshot after every stage (CLAUDE.md §9a)")
        sp.add_argument(
            "--prior",
            dest="prior",
            action="store_true",
            default=None,
            help=(
                "Enable the network (SIMBAD) catalog prior for IRCUT galaxy-vs-starfield "
                "(galaxy-vs-globular). The offline local catalog identity/category lookup "
                "always runs on a plate-solved frame regardless of this flag."
            ),
        )
        sp.add_argument(
            "--no-prior",
            dest="prior",
            action="store_false",
            help=(
                "Disable ONLY the network (SIMBAD) lookup, even if config enables it. The "
                "offline local catalog identity/category lookup is always on for a "
                "plate-solved frame and is not affected by this flag."
            ),
        )

    sp = sub.add_parser("discover", help="List targets under a library or single-target folder")
    sp.add_argument("library_dir")

    sp = sub.add_parser("run", help="Render ONE target folder (S30 native layout) into --out")
    sp.add_argument("target_dir")
    sp.add_argument("--out", required=True, help="output directory for this target")
    sp.add_argument(
        "--recipe",
        default=None,
        help="replay: an edited <TARGET>_recipe.json whose your_value pins to honor "
        "(explicit only; recipes are never auto-discovered)",
    )
    _add_render_opts(sp)

    sp = sub.add_parser("process", help="Discover + render every target under a library")
    sp.add_argument("library_dir")
    sp.add_argument("--out", required=True, help="output ROOT; each target renders to <out>/<target>/")
    _add_render_opts(sp)

    sp = sub.add_parser("prior", help="WCS catalog prior for a FITS (cone-search SIMBAD)")
    sp.add_argument("fits")
    sp.add_argument("--radius", type=float, default=None, help="cone radius (deg)")

    sp = sub.add_parser("config", help="Explain preferences or generate a preferences.yaml template")
    sp.add_argument(
        "action",
        choices=["explain", "template"],
        help="explain: show current preset values; template: write a preferences.yaml",
    )
    sp.add_argument("param", nargs="?", default=None, help="(explain only) a single preference name, or omit for all")
    sp.add_argument("--out", default=None, help="(template only) path to write the file; prints to stdout if omitted")

    sub.add_parser("doctor", help="Check that all required tools can be found and print where")

    sp = sub.add_parser("qa", help="Score the deliverables in a target's output directory")
    sp.add_argument("proc_dir")
    sp.add_argument("--target", default=None, help="target name (default: output dir name)")

    sp = sub.add_parser("validate", help="Back-validate: re-render validation targets, diff QA vs committed baselines")
    sp.add_argument("--targets", default=None, help="comma-separated subset (default: all configured)")
    sp.add_argument("--update-baselines", action="store_true", help="capture fresh QA as the new baselines")
    sp.add_argument("--scratch", default=None, help="override validation.scratch_dir")

    sp = sub.add_parser("benchmark", help="Score one render against references")
    sp.add_argument("render")
    sp.add_argument("--ref-default", help="content reference (S30 default)")
    sp.add_argument("--ref-aesthetic", help="aesthetic/tone reference")
    sp.add_argument("--star-floor", type=int, help="hard-fail below this star count")

    args = p.parse_args(argv)

    if args.cmd == "inspect":
        from .inspect import inspect
        from .io import load_config

        ccfg = (load_config().get("engine") or {}).get("classify")
        print(json.dumps(inspect(args.fits, classify_cfg=ccfg), indent=2))
        return 0

    if args.cmd == "discover":
        from .run import discover_targets

        d = discover_targets(args.library_dir)
        for t in d["targets"]:
            print(
                f"{t['target']:12s} {t['filter'] or '?':6s} "
                f"{t['sessions']} session(s), latest {t['datetime']}  ({t['folder']})"
            )
        for s in d["skipped"]:
            print(f"skipped: {s['path']} — {s['reason']}")
        print(f"\n{len(d['targets'])} target(s).")
        return 0

    if args.cmd == "doctor":
        from .io import load_config
        from .tools import doctor

        print(doctor(load_config()))
        return 0

    # All remaining commands need the config.
    from .io import load_config

    cfg = load_config()

    if args.cmd == "run":
        from .adapters.base import AdapterError
        from .execute import ExecutionError
        from .orchestrate import PipelineError
        from .recipe import RecipeError
        from .run import run_target

        # A non-showcase profile is a labeled FORK -> name its hero {target}_{profile} so it
        # sits ALONGSIDE the showcase {target}_final instead of overwriting it.
        out_name = "final" if args.profile == "showcase" else args.profile
        try:
            s = run_target(
                args.target_dir,
                args.out,
                cfg,
                profile=args.profile,
                debug=args.debug,
                catalog_prior=args.prior,
                out_name=out_name,
                recipe_path=args.recipe,
            )
        except RecipeError as e:
            print(e)
            return 2
        except (AdapterError, ExecutionError, PipelineError) as e:
            # Diagnosis 7: a tool failure or planning refusal is an EXPLAINED
            # stop, not a crash -- print the bounded, actionable message on
            # STDERR (so a shell pipeline can separate it from the report on
            # stdout) and exit non-zero. An untyped exception still raises, so a
            # genuine defect keeps its traceback.
            print(f"{args.target_dir}: {e}", file=sys.stderr)
            return 1
        # Owner ruling (2026-07-31): mirror `process` — a completed render whose
        # status is neither "pass" nor "source_limited" is a failed command.
        if s.get("status") not in ("pass", "source_limited"):
            _print_status_failure(s)
            _print_replay_report(s)
            return 1
        _print_run(s)
        _print_replay_report(s)
        return 0

    if args.cmd == "process":
        from .run import discover_targets, process_library

        out_name = "final" if args.profile == "showcase" else args.profile
        d = discover_targets(args.library_dir)
        for s in d["skipped"]:
            print(f"skipped: {s['path']} — {s['reason']}")
        batch = process_library(
            args.library_dir,
            args.out,
            cfg,
            profile=args.profile,
            debug=args.debug,
            catalog_prior=args.prior,
            out_name=out_name,
        )
        for s in batch.successes:
            _print_run(s)
        for s in batch.status_failures:
            _print_run(s)
            _print_status_failure(s)
        for f in batch.failures:
            print(f"\n{f.target} [{f.folder}] — FAILED: {f.error_type}: {f.message}")
            print(f"  earlier and other successful outputs were kept in {args.out}")

        # Failure classification (exception vs. non-"pass"/"source_limited" status)
        # lives in BatchRunResult itself (owner-ratified 2026-07-31 layering fix) --
        # the CLI only consumes batch.failures/batch.status_failures/batch.ok, it
        # does not re-derive the classification from each result's `status`.
        n_ok = len(batch.successes)
        n_failed = len(batch.failures) + len(batch.status_failures)

        if not batch.ok:
            print(f"\nProcessed {n_ok} target(s); {n_failed} failed. Successful outputs were kept in {args.out}")
            return 1
        print(f"\nProcessed {n_ok} target(s) into {args.out}")
        return 0

    if args.cmd == "prior":
        from . import prior as PR
        from .inspect import read_header

        if args.radius is not None:
            cfg = {**cfg, "prior": {**cfg.get("prior", {}), "radius_deg": args.radius}}
        print(json.dumps(PR.prior_for(args.fits, read_header(args.fits), cfg), indent=2))
        return 0

    if args.cmd == "config":
        from .prefs import explain, generate_template

        if args.action == "explain":
            print(explain(args.param))
        else:
            tmpl = generate_template()
            if args.out:
                with open(args.out, "w", encoding="utf-8") as f:
                    f.write(tmpl)
                print(f"Written to {args.out}")
            else:
                print(tmpl)
        return 0

    if args.cmd == "validate":
        from .validate import run_validation

        wanted = [t.strip() for t in args.targets.split(",")] if args.targets else None
        summary = run_validation(cfg, targets=wanted, update=args.update_baselines, scratch=args.scratch)
        for r in summary["results"]:
            print(f"\n{r['target']}: {r['status'].upper()}" + (f"  ({r['reason']})" if r.get("reason") else ""))
            if r["status"].startswith("replay_"):
                if r["status"] != "replay_skipped":
                    px = r.get("pixels") or {}
                    print(
                        f"  reproducibility: params {'identical' if not r.get('param_diffs') else 'DIFFER: ' + str(r['param_diffs'])}"
                    )
                    print(f"  pixels: max_abs={px.get('max_abs')} rmse={px.get('rmse')} (tol {r.get('tolerances')})")
                continue
            d = r.get("diff")
            if not d:
                continue
            print(f"  {'metric':22s} {'baseline':>10s} {'fresh':>10s} {'delta':>9s} {'tol':>8s}  ok")
            for name, m in d["metrics"].items():
                if m["ok"] is None:
                    print(f"  {name:22s} {'—':>10s} {'—':>10s} {'':>9s} {'':>8s}  skipped")
                else:
                    # A demoted metric prints `diag` -- reported, never silently dropped.
                    verdict = "diag" if m.get("diagnostic") else ("ok" if m["ok"] else "FAIL")
                    print(
                        f"  {name:22s} {m['baseline']:>10.4g} {m['fresh']:>10.4g} "
                        f"{m['delta']:>9.4g} {m['tolerance']:>8.4g}  {verdict}"
                    )
            h = d["hard"]
            if h["passed_regressed"]:
                print("  REGRESSION: baseline passed, fresh does not")
            if h["new_gate_fail_kinds"]:
                print(f"  REGRESSION: new gate-fail kinds {h['new_gate_fail_kinds']}")
            if h["resolved_gate_fail_kinds"]:
                print(f"  improved: resolved gate-fail kinds {h['resolved_gate_fail_kinds']}")
            for w in d["warnings"]:
                print("  warn:", w)
        print(f"\nSummary: {summary['counts']}  ->  {'OK' if summary['ok'] else 'REGRESSION'}")
        return 0 if summary["ok"] else 2

    if args.cmd in ("qa", "benchmark"):
        # Split dispatch: only `benchmark` needs the resolved tools (for score()).
        # `qa_target` resolves what it needs itself, so pre-resolving here would just
        # make `seestar qa` resolve tools twice (and pointlessly require all four on a
        # box that only wants to score existing deliverables).
        if args.cmd == "qa":
            from .qa import qa_target

            sc = qa_target(args.proc_dir, cfg, args.target)
        else:
            from .qa import score
            from .tools import resolve_tools

            sc = score(
                args.render,
                ref_default=args.ref_default,
                ref_aesthetic=args.ref_aesthetic,
                star_floor=args.star_floor,
                tools=resolve_tools(cfg),
            )
        print(json.dumps(sc, indent=2))
        hero = sc.get("hero") or {}
        if hero.get("outcome"):
            print(f"\nHERO OUTCOME: {hero['outcome']}")
            for r in hero.get("reasons", []):
                print(f"  - {r}")
        # Task 13: an UNCERTIFIED evaluation (Task 11's `sc["publication"]`) must
        # never print a bare PASS -- a passing recomputation over an uncertified
        # publication is not certification, and a plain PASS would look identical
        # to one. Print the manifest's own recorded outcome instead and fail closed
        # (exit 2) regardless of whether the recomputation itself passed.
        pub = sc.get("publication")
        if isinstance(pub, dict) and pub.get("certified") is False:
            print(f"\nUNCERTIFIED (manifest outcome: {pub.get('manifest_outcome')})")
            rc = 2
        else:
            print("\nPASS" if sc["passed"] else "\nHARD FAIL: " + "; ".join(sc["hard_fails"]))
            rc = 0 if sc["passed"] else 2
        for w in sc["warnings"]:
            print("warn:", w)
        return rc

    return 1


if __name__ == "__main__":
    sys.exit(main())
