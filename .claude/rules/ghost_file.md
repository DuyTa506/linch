# AI Agent Rule: The Shadow File Technique

## 1. Rationale (Why we use this)
AI agents often struggle and make mistakes (e.g., hallucinated code, indentation errors, context loss) when attempting to edit or patch large, existing files in-place. Agents are inherently much better and more accurate at generating new files from scratch. The Shadow File Technique leverages this strength by treating complex modifications as new file generation.

## 2. Triggers (When to use)
Automatically apply this technique in the following scenarios:
- Modifying multiple scattered locations within an existing file.
- Making extensive, large-scale modifications to an existing file.
- Generating a completely new, massive file.

## 3. Execution Strategy (How to execute)
Strictly follow these steps without deviation:

- Step 1: Create, Do Not Copy: Create a completely new temporary file (e.g., filename.ext.shadow). DO NOT copy the existing contents of the old file into it. Start generating the final, updated state of the code directly into this new shadow file.
- Step 2: Chunked Writing (Append): If the new file is too large to write in a single output/turn, write it in parts. Append each subsequent section sequentially to the shadow file until the entire file is completely written.
- Step 3: Verify: Inspect the shadow file to ensure all parts are completely written and the syntax is correct.
- Step 4: Rename & Preserve History: Once the shadow file is fully verified, delete the original file and rename the shadow file to the exact original filename (e.g., rm filename.ext && mv filename.ext.shadow filename.ext). This replacement ensures Git correctly tracks the changes as an update, perfectly preserving the Git history and git blame.