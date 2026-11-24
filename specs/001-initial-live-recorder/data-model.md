# データモデル

内部物理名や列集合の基準は[技術設計のDM節](../../docs/system-design.md)とする。ここでは工程間で共通の制約を指定する。

| 正規フィールド                       | 制約                                                                                      |
| ------------------------------------ | ----------------------------------------------------------------------------------------- |
| Streamer.identity                    | サービスと正規化IDの組を一意とし、同一正規化IDは媒体フォルダをサービス間で共有する。      |
| StreamSession.source_identity        | サービスの配信IDを優先。同じ配信の再接続で重複セッションを作らない。                      |
| RecordingSegment.number              | セッション内一意で1から開始する。確定済みを改番しない。                                   |
| RecordingSegment.media_generation    | 確定媒体の所有・世代を公開や破壊的操作前に照合する。                                      |
| Comment.recording_offset_ms          | 区間の起点に基づく。対応付けを確定できない場合は不明を保持する。                          |
| SubtitlePublication.input_generation | セグメントと入力版を照合して公開する。旧版の再生成と期限削除済みASSの復活防止を担保する。 |
| SpeechCue.offset_range               | 確定動画のタイムラインに収まる開始・終了を保持する。                                      |
| RuntimeConfig.web_port               | 固定の整数1024〜65535。通常8082、隔離8083。変更は次回起動から。                           |
| SystemEvent.correlation_id           | セッション・ジョブ等の障害追跡用。非秘匿の本文を保持する。                                |
| DeletionOperation.generation         | 対象の世代を記録し、別版・管理外媒体の削除を拒否する。                                    |
| RequestBudget.window                 | 共有単位と期間ごとの消費済み・不明・待機をDB復元から独立して保持する。                    |
