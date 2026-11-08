---
name: settings-toggle
title: Observe and set a named control
description: Toggle the unique requested control only when its current state differs.
allowed-tools: android_get_ui_tree,android_tap
---

Read the current UI. Identify the unique control matching the objective text. If its value already matches the requested state, finish. Otherwise tap it once, then re-observe. Never change another control. If the match is ambiguous, stop.
