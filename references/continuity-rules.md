# Continuity rules

Record only story-critical, visually persistent state. Store it in `entity_state_timeline.json` and bind every supported change to a novel range or reviewed comic evidence.

## Evidence priority

Use this order: novel fact, character reference, reviewed adjacent page, reviewed established page, then explicit inference. Unknown is a real state; do not turn unknown into an invented detail.

## Character identity

Track face shape, skin tone, hair color and style, headwear, body build, scars, injuries, and persistent accessories. Facial hair is identity state: record beard, moustache, sideburn, stubble, color, length, and shape when visible. A beard appearing or disappearing requires a supported transition; camera crop is not a disappearance.

Character sheets define identity, not comic rendering style. A reviewed comic page supplies line work, color treatment, texture, simplification, and detail density.

## Character appearance matrix

Before page classification, build one `character_appearance_matrix.json` per semantic cluster and validate it with `scripts/validate_appearance_matrix.py`. Bind every named character to the exact identity-reference hash and list every owned cluster page in order. For every page, inspect the full-resolution face plus any visible neck, chest, and arms; record `match`, `drift`, or `not_visible` for skin tone, hair, facial hair, and clothing.

Use the character reference as the identity baseline. Water, shadow, mood lighting, and watercolor texture may shift local color, but they do not justify a change of skin-tone category or undertone across one continuous scene. Record a concrete lighting explanation for every observation. A visible character cannot use `not_visible`; an unobserved page, missing reference, missing crop evidence, or any unresolved drift blocks confirmation and page promotion.

## Clothing, props, extras, and scenes

For clothing and props, use `present`, `absent`, or `unknown`, plus owner, material, appearance, and condition when known. Add or remove an item only when story state supports it. Track recurring extras with stable identity even without a character sheet.

For scenes, track location, time, weather, entrances, exits, axis, important furniture, and persistent damage. Context pages may inform a decision but never change cluster ownership.

## What is not a defect

Non-critical action variation is not a defect. Different grip, hand count, pose, camera angle, or expression is acceptable when the same story action and continuity state remain clear. Do not redraw a correct person merely to imitate every verb in the novel.

## Text and speaker state

Build a speaker graph for every ordinary-text block. Keep art text and sound effects separate. A missing or uncertain speaker blocks promotion. Text density may be reduced only with source-faithful wording and without losing required plot information.
