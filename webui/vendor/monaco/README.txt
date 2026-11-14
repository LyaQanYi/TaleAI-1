monaco-editor 0.52.2（npm，MIT）
只保留 min/vs 里编辑 Markdown 用得到的部分：loader、editor、base、basic-languages（Markdown 会嵌 XML，代码块会嵌各种语言，整个带上约 650KB）、中文界面文案。
去掉了 language/（JSON/CSS/HTML/TS 语言服务，约 7MB）和其他语言的界面文案。
