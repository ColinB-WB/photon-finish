# Fine-tune one image

Fine-tuning is optional. Most users should first try the default result and the
other [output styles](styles-and-versions.md).

After a successful single-target render, the app reveals **Advanced: manually
adjust this image**. The adjustment file is a recipe card: it explains each
setting, the value the engine chose, the tool that applies it, why it matters,
and what a wrong value tends to look like.

## Adjust in the app

1. Expand **Advanced: manually adjust this image**.
2. Choose **Open adjustment file**.
3. Find an entry whose explanation matches what you want to change.
4. Replace its `"your_value": null` with the number or value you want to try.
5. Save the file without changing its JSON punctuation.
6. Return to the app and choose **Re-create using saved adjustments**.

Only settings with a non-null `your_value` are pinned. Everything else is
measured again. The ordinary **Create finished photo** button always starts
clean and does not silently reuse your edits.

The result area includes an adjustment-details table showing prior and current
values and which values came from you.

## Adjust from the command line

```console
seestar run <target-folder> \
  --out <target-output-folder> \
  --recipe <target>_recipe.json
```

The recipe can be stored anywhere when supplied explicitly.

## Recover from an invalid edit

If the recipe no longer parses:

1. undo the last edit in your text editor; or
2. set the edited `your_value` back to `null`; or
3. choose **Create finished photo** to make a clean automatic render and emit a
   fresh recipe.

Do not edit the generated TIFF, PNG, JPG, notes, or catalog JSON as a substitute
for a recipe. Generated files may be replaced by a later render.
