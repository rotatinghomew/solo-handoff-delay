# 联络接力的等待归因与服务承诺重订

数据来自独立设计的合成业务记录，不含真实人员资料。文件为 UTF-8 JSON。

## 资料字典

`requests` 的 `join` 表示需要全分支还是任一分支完成；退回后没有有效履行。独立的家长和学生请求不能凭户号互相办结；共同凭据只有明确列了请求关联才可共用。`events` 的 `day` 是实际发生日，`evidence` 相同的转抄不另算；发出与接手之间仍是原联络台。`source_gaps` 是提供方未能出证的日段，不是部门已办或未办的证明。`windows` 是已经约定或正在拟定的个人服务时段；草稿由 `rebook_requests` 给出。改约占用有明确 `capacity` 的承接窗口，同一时段有限的可选名额不能重复分配。`pauses` 只在注明由家庭同意且人与事项吻合时暂停该方等待；等待的周界按每七日，`report_days` 为可用截点，准时判断按受理承诺 `deadline`。`amendments` 的日期是知道更正的日子。

## 读取与核对

使用 Python 3.11。
资料读取只需标准库。

用 `python3 -m json.tool data/records.json` 查看原始记录。用 `python3 -m unittest discover -s tests -v` 核对演练资料的格式及关联。仓库目前只提供资料与输入核对，不提供业务分析结果。
