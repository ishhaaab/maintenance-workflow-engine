# Report dependencies and font notices

PDF reports use the pinned ReportLab dependency under its BSD license. The installed distribution retains its complete license in `reportlab-5.1.0.dist-info/licenses/LICENSE`:

```text
Copyright (c) 2000-2024, ReportLab Inc.
All rights reserved.

Redistribution and use in source and binary forms, with or without modification,
are permitted provided that the following conditions are met:

* Redistributions of source code must retain the above copyright notice,
  this list of conditions and the following disclaimer.
* Redistributions in binary form must reproduce the above copyright notice,
  this list of conditions and the following disclaimer in the documentation
  and/or other materials provided with the distribution.
* Neither the name of the company nor the names of its contributors may be
  used to endorse or promote products derived from this software without
  specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED.
IN NO EVENT SHALL THE OFFICERS OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED
TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS;
OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER
IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING
IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
SUCH DAMAGE.
```

`src/workflow_engine/fonts/NotoSans-Regular.ttf` and `NotoSans-Bold.ttf` are Noto Sans. The accompanying license identifies Copyright 2018 The Noto Project Authors. They are redistributed unchanged under the SIL Open Font License 1.1. The complete copyright/license is bundled beside the fonts in [`OFL.txt`](../src/workflow_engine/fonts/OFL.txt). The application embeds their subsets in generated PDF reports. Noto Sans does not cover every Unicode character; unsupported PDF characters produce an explicit error with CSV fallback.
