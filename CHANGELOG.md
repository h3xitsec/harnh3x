# Changelog

## [0.8.0](https://github.com/h3xitsec/harnh3x/compare/harnh3x-v0.7.0...harnh3x-v0.8.0) (2026-10-22)


### Features

* **agent:** cache Claude prompts between requests ([52ed779](https://github.com/h3xitsec/harnh3x/commit/52ed779776ce01fa2a1becef606dca2ea5b91010))
* explore the codebase with a read-only subagent ([7a24676](https://github.com/h3xitsec/harnh3x/commit/7a246768a88242e7105b9e7d2659e2e3a730281c))
* **tui:** show prompt cache use in the status bar ([c5b443b](https://github.com/h3xitsec/harnh3x/commit/c5b443bd252db407e38b25c9f5c90c2f3a4e79f4))
* **tui:** use the Dracula palette and a bordered prompt ([f6edc39](https://github.com/h3xitsec/harnh3x/commit/f6edc392f1fb61cab6fdf67e6ad879642d656678))


### Bug Fixes

* **tui:** hide turn headings with the rest of the trace ([2be2426](https://github.com/h3xitsec/harnh3x/commit/2be24265f0e03afdc8297a41a21808e88b8351e2))
* **tui:** show the model and workspace only once ([13e59e0](https://github.com/h3xitsec/harnh3x/commit/13e59e083b8613fcc7f884b7e4baa3eeebcef3c3))


### Performance Improvements

* **prompt:** keep the system prompt cacheable when a skill loads ([b7c2b38](https://github.com/h3xitsec/harnh3x/commit/b7c2b38e47a9ce878cdcc4ec20b4aaf338ef891b))

## [0.7.0](https://github.com/h3xitsec/harnh3x/compare/harnh3x-v0.6.0...harnh3x-v0.7.0) (2026-10-21)


### Features

* ask to continue when the turn limit is reached ([0d0283d](https://github.com/h3xitsec/harnh3x/commit/0d0283d9fee6fe680fc05857b8d9ef1986dd1c90))
* **cli:** ask for a model when the configured one is missing ([b91ab11](https://github.com/h3xitsec/harnh3x/commit/b91ab115ec1a076ca2f111565cfae727eb0d1696))
* **cli:** start a coding session with --coding PATH ([b0c6739](https://github.com/h3xitsec/harnh3x/commit/b0c673948fcfd7680137eee3784eed5599856878))
* **config:** set OpenAI and Anthropic credentials separately ([25f510d](https://github.com/h3xitsec/harnh3x/commit/25f510d9e46935b5d3f45b0726fd576a4137b996))
* label each model round in the transcript ([79c675e](https://github.com/h3xitsec/harnh3x/commit/79c675ede431115f3bb205b79daf96987b83c024))
* **sandbox:** run sandboxed shell and Python on Linux ([84227bd](https://github.com/h3xitsec/harnh3x/commit/84227bd5a2d100476ac1158e9a65dccada01d683))
* **skills:** load your own skills from ~/.harnh3x/skills ([0cfcddc](https://github.com/h3xitsec/harnh3x/commit/0cfcddc840df906b2d12a5ba2628ed58b149040d))
* **tools:** find files and text with glob and grep ([fa7328b](https://github.com/h3xitsec/harnh3x/commit/fa7328b385bf3acd014d3b9f99726ee279acf7c5))
* **tools:** outline Python classes and functions with line spans ([0ede2d5](https://github.com/h3xitsec/harnh3x/commit/0ede2d5856e9a710d870d3ea6876c99dcce2436c))
* **tui:** close status bar boxes by clicking outside them ([191772b](https://github.com/h3xitsec/harnh3x/commit/191772b486f9f341591c6145e7df0d87581276eb))
* **tui:** list loaded skills and their tools from the status bar ([7ef4b55](https://github.com/h3xitsec/harnh3x/commit/7ef4b554eefdad474ce4508f0748f750c3fd55d3))
* **tui:** pick a project file or folder with @ ([bf7f06b](https://github.com/h3xitsec/harnh3x/commit/bf7f06b52cfe407344591624978e2a3184ad5d2a))
* **tui:** show the status bar as compact badges that fit narrow terminals ([964e7df](https://github.com/h3xitsec/harnh3x/commit/964e7df6f6d6f5b6d972d19fa44ac25254eae82d))
* **tui:** switch models by clicking the model name in the status bar ([9a044a6](https://github.com/h3xitsec/harnh3x/commit/9a044a6829fec1f6f3f622a59e407b20b8e40a5e))


### Documentation

* use a placeholder model name in the example config ([4c6c189](https://github.com/h3xitsec/harnh3x/commit/4c6c1898b7d2b6dfbd3f506c2d1fe97a12c0bb7b))

## [0.6.0](https://github.com/h3xitsec/harnh3x/compare/harnh3x-v0.5.1...harnh3x-v0.6.0) (2026-10-17)


### Features

* **agent:** send max_tokens on OpenAI completions ([1908536](https://github.com/h3xitsec/harnh3x/commit/1908536605dc28dccd374ba434ee9c9ed3b14d86))
* **cli:** start a new session with /new and hide trace boxes with /details ([4e9aa4d](https://github.com/h3xitsec/harnh3x/commit/4e9aa4d9f2f0726999858d4c6a69163f675f1a56))
* export or copy a response ([58bd853](https://github.com/h3xitsec/harnh3x/commit/58bd8531135193af8e58b41019f9a829439375d9))
* **skills:** describe MCP servers from config instructions and what they report ([0e8b60f](https://github.com/h3xitsec/harnh3x/commit/0e8b60f3375af7b6bfe79007e607b81d2f2f283f))
* **skills:** load coding once to list, search, edit, and run project commands ([14cecfc](https://github.com/h3xitsec/harnh3x/commit/14cecfce4d6580c5cd097f29bb879c1b55249b12))
* **skills:** tell the model each tool's sandbox or yolo limits on every call ([14cecfc](https://github.com/h3xitsec/harnh3x/commit/14cecfce4d6580c5cd097f29bb879c1b55249b12))
* **tools:** cap shell and Python output with shell_max_output ([2dc2f6d](https://github.com/h3xitsec/harnh3x/commit/2dc2f6df15c57ceeac21ad8dede5877fcfc0c6e1))
* **tools:** report the host OS, CPU, memory, and hardware ([7150554](https://github.com/h3xitsec/harnh3x/commit/7150554aceb82e8b2fb497ba9217da6ba9cf8aa9))
* **tools:** run pipes and redirects in yolo mode ([cc3126c](https://github.com/h3xitsec/harnh3x/commit/cc3126cf4107e670efbeac5e9154fd6a365db3f7))
* **tui:** count MCP servers and their tools apart from built-in ones ([027b11c](https://github.com/h3xitsec/harnh3x/commit/027b11cdc427ceb71159a1e6718b63e34e21da52))
* **tui:** filter /resume by typing and prefill the export file name ([4e9aa4d](https://github.com/h3xitsec/harnh3x/commit/4e9aa4d9f2f0726999858d4c6a69163f675f1a56))
* **tui:** grow the composer, recall past prompts, and complete slash commands ([4e9aa4d](https://github.com/h3xitsec/harnh3x/commit/4e9aa4d9f2f0726999858d4c6a69163f675f1a56))
* **tui:** mark each tool call as running, done, or failed with its error ([4e9aa4d](https://github.com/h3xitsec/harnh3x/commit/4e9aa4d9f2f0726999858d4c6a69163f675f1a56))
* **tui:** open MCP warning details by clicking the status bar ([4e9aa4d](https://github.com/h3xitsec/harnh3x/commit/4e9aa4d9f2f0726999858d4c6a69163f675f1a56))
* **tui:** show context use as a percentage that turns yellow, then red, near compaction ([4e9aa4d](https://github.com/h3xitsec/harnh3x/commit/4e9aa4d9f2f0726999858d4c6a69163f675f1a56))
* **tui:** show copy and export results as toasts and failed answers in red ([4e9aa4d](https://github.com/h3xitsec/harnh3x/commit/4e9aa4d9f2f0726999858d4c6a69163f675f1a56))
* **tui:** stop a running turn with Esc and show what it is doing ([4e9aa4d](https://github.com/h3xitsec/harnh3x/commit/4e9aa4d9f2f0726999858d4c6a69163f675f1a56))


### Bug Fixes

* **agent:** answer every pending tool call when a turn is aborted ([f328650](https://github.com/h3xitsec/harnh3x/commit/f328650e16416b84aff762546897734393c82af6))
* **agent:** honour context_limit for OpenAI-compatible servers ([c06ed95](https://github.com/h3xitsec/harnh3x/commit/c06ed959993eff71ed4fb9aadcc65f2e0933e196))
* **agent:** keep the compacted-context estimate after a summary ([a949129](https://github.com/h3xitsec/harnh3x/commit/a949129aadaed515f21f591205f0394278b7bfd4))
* **config:** reject out-of-range counts, limits, and ratios at startup ([08212eb](https://github.com/h3xitsec/harnh3x/commit/08212eb55fb6c52ebbd821f5befcb6a8b75524db))
* **http:** connect only to an address that passed the host check ([6b65fa3](https://github.com/h3xitsec/harnh3x/commit/6b65fa3e55ed99cabd0b1016e23e10028bf1e3c9))
* **skills:** register one exit handler for MCP processes ([f511a50](https://github.com/h3xitsec/harnh3x/commit/f511a50903d44801842f04336661e03613aa5b19))
* **skills:** stop the API rejecting a turn when two loaded skills share a tool ([14cecfc](https://github.com/h3xitsec/harnh3x/commit/14cecfce4d6580c5cd097f29bb879c1b55249b12))
* **tools:** refuse a .docx whose document part exceeds the byte cap ([3d78d52](https://github.com/h3xitsec/harnh3x/commit/3d78d52dad6e88590e67edced898dd2e103ddd39))
* **tools:** stop read_url at the PDF page cap ([356366a](https://github.com/h3xitsec/harnh3x/commit/356366a7012f54966d8cf0f5e28cea70880b0299))
* **tui:** close MCP servers on Ctrl+Q ([c4d3a10](https://github.com/h3xitsec/harnh3x/commit/c4d3a104b814feb78f5a26058283ee5fc0c58c69))
* **tui:** make Ctrl+H, Shift+Enter, and Option+Backspace work in Terminal.app ([4e9aa4d](https://github.com/h3xitsec/harnh3x/commit/4e9aa4d9f2f0726999858d4c6a69163f675f1a56))


### Performance Improvements

* **tui:** scroll smoothly while an answer is streaming ([8c6d991](https://github.com/h3xitsec/harnh3x/commit/8c6d99170e9256550d40e9058e35bce6e3ddc16f))

## [0.5.1](https://github.com/h3xitsec/harnh3x/compare/harnh3x-v0.5.0...harnh3x-v0.5.1) (2026-10-15)


### Bug Fixes

* **tui:** stay up when clicking a streaming answer ([e31fc7a](https://github.com/h3xitsec/harnh3x/commit/e31fc7abcb9e917977df59e9139aed054ddb6eee))

## [0.5.0](https://github.com/h3xitsec/harnh3x/compare/harnh3x-v0.4.0...harnh3x-v0.5.0) (2026-10-14)


### Features

* **skills:** read skills from Agent Skills frontmatter ([42c5c77](https://github.com/h3xitsec/harnh3x/commit/42c5c7712e62ea9b8ca54972658e7221f9d6de03))


### Bug Fixes

* quit while a request is still running ([fbc5ea0](https://github.com/h3xitsec/harnh3x/commit/fbc5ea085bb4177b33606c15da84b9c153a6010c))
* **tui:** keep selected text in an expanded panel ([43ab027](https://github.com/h3xitsec/harnh3x/commit/43ab0275c9e18e78c919a723f8509abbebc99a56))


### Miscellaneous Chores

* release 0.5.0 ([78e5e89](https://github.com/h3xitsec/harnh3x/commit/78e5e8965e7a19011ef1f71b86b1198e140d941e))

## [0.4.0](https://github.com/h3xitsec/harnh3x/compare/harnh3x-v0.3.0...harnh3x-v0.4.0) (2026-10-13)


### Features

* load MCP servers from the YAML config as skills ([51f63aa](https://github.com/h3xitsec/harnh3x/commit/51f63aa0514a95cd93a5780255f48d929062b0c7))
* **skills:** load skills that live in category folders ([c25f9c7](https://github.com/h3xitsec/harnh3x/commit/c25f9c71103304bc9e68a89b99e9afa40ba278b5))
* **skills:** omit an MCP skill when its server is not accepting connections ([0a567bc](https://github.com/h3xitsec/harnh3x/commit/0a567bc3111cb3ef6d435f9b7e9de5fa8a60eea0))
* **tools:** reach localhost and private hosts in yolo mode ([a5a77b4](https://github.com/h3xitsec/harnh3x/commit/a5a77b4c0df4fc4b7008480014dfd1dadc4872ae))
* **tools:** send HTTP requests and return status, headers, and body ([d45159c](https://github.com/h3xitsec/harnh3x/commit/d45159cf4058daf4a182b5a17715f94a6ddba1be))
* **tui:** show model, context, and MCP warnings on a status bar ([c588fa9](https://github.com/h3xitsec/harnh3x/commit/c588fa97b8e2b913cc52497d8ffc1edba972bafb))
* **tui:** show the package version at the right of the status bar ([3a6f3d0](https://github.com/h3xitsec/harnh3x/commit/3a6f3d091ff58cde2525bd9d6a19827bde080e60))


### Bug Fixes

* **skills:** name the real tools when a skill is called as a tool ([c8a9a22](https://github.com/h3xitsec/harnh3x/commit/c8a9a22d53da22ff5e95d78693301bf4eb472829))

## [0.3.0](https://github.com/h3xitsec/harnh3x/compare/harnh3x-v0.2.0...harnh3x-v0.3.0) (2026-10-12)


### Features

* edit a granted project in a coding session ([948a7eb](https://github.com/h3xitsec/harnh3x/commit/948a7eb2e0dc8d7566b36d3daa61e2fa3530df7e))
* show the prompt and each request with --debug ([4453292](https://github.com/h3xitsec/harnh3x/commit/445329239436135e5f72de4e14994e69b32fe0dd))
* **tui:** collapse tool calls until you click the box ([44ce184](https://github.com/h3xitsec/harnh3x/commit/44ce1845a69837d14de4fe9afa46f15f522d00c8))
* **tui:** show the version next to Harnh3x ([a3411f5](https://github.com/h3xitsec/harnh3x/commit/a3411f5e5ee8938f9dafc2db5533210949960cb2))


### Bug Fixes

* **tui:** keep your place while the answer is streaming ([7b19903](https://github.com/h3xitsec/harnh3x/commit/7b199031cff46b393d334ea563439bc938724e00))


### Documentation

* correct the pipx install repository URL ([613144f](https://github.com/h3xitsec/harnh3x/commit/613144fbd907ff6fe69459248206d0d199d0c167))

## [0.2.0](https://github.com/h3xitsec/harnh3x/compare/harnh3x-v0.1.0...harnh3x-v0.2.0) (2026-10-12)


### Features

* talk to Claude through the Anthropic API ([dca6554](https://github.com/h3xitsec/harnh3x/commit/dca655406007b79712ed67eb91d0ca1084192c1a))
* talk to Claude through the Anthropic API ([6efb6d1](https://github.com/h3xitsec/harnh3x/commit/6efb6d171f877e0bab1e66a60109f66e138938a1))

## 0.1.0 (2026-10-12)


### Continuous Integration

* cut the first release ([491e9dc](https://github.com/h3xitsec/harnh3x/commit/491e9dc333802d80233224d0ec10d0939e3a1f4c))
