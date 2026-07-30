from __future__ import annotations

CAMPAIGN_TEXT_DEFAULTS = {
    "campaign_group_required": "该功能只能在 QQ 群内使用。",
    "campaign_feature_disabled": "当前群未启用该功能。",
    "campaign_invalid_argument": "参数格式错误，请发送对应的帮助指令查看用法。",
    "newapi_test_success": (
        "New API 连接测试成功。\n"
        "版本：{version}\n账号：{username}\n角色：{role}\n额度显示：{display_type}"
    ),
    "newapi_error": "New API 操作失败：{error}",
    "newapi_user_error": "New API 用户校验失败：{error}",
    "lottery_help": (
        "抽奖管理指令：\n"
        "/抽奖 创建 <标题>\n"
        "/抽奖 时间 <开始时间> <开奖时间>\n"
        "/抽奖 口令 <报名关键词>\n"
        "/抽奖 描述 <内容>\n"
        "/抽奖 奖项添加 <奖项名> <人数> <额度>\n"
        "/抽奖 奖项删除 <序号>\n"
        "/抽奖 领奖时限 <时长>\n"
        "/抽奖 发布\n/抽奖 状态\n/抽奖 参与者 [页码]\n"
        "/抽奖 提前开奖\n/抽奖 取消 [原因]\n"
        "/抽奖 核查 <流水号> <成功|失败>"
    ),
    "lottery_invalid_argument": "抽奖指令参数错误，请发送 /抽奖 帮助。",
    "lottery_active_exists": "本群已有一场未结束的抽奖活动。",
    "lottery_created": (
        "抽奖草稿已创建（#{activity_id}）：{title}\n"
        "开始：{start_time}\n开奖：{draw_time}\n报名口令：{keyword}"
    ),
    "lottery_no_draft": "本群没有可编辑的抽奖草稿。",
    "lottery_updated": "抽奖草稿已更新。",
    "lottery_keyword_reserved": "该口令与新人礼、抽奖领奖或补偿指令冲突，请更换。",
    "lottery_prize_added": (
        "已添加奖项 {position}：{prize_name} ×{winner_count}，额度 {amount}。"
    ),
    "lottery_prize_deleted": "已删除第 {position} 个奖项。",
    "lottery_prize_not_found": "没有找到第 {position} 个奖项。",
    "lottery_invalid_time": "开奖时间必须晚于开始时间。",
    "lottery_no_prizes": "发布前至少需要添加一个奖项。",
    "lottery_published": (
        "抽奖活动已发布（#{activity_id}）：{title}\n"
        "简介：{description}\n"
        "报名时间：{start_time} 至 {draw_time}\n"
        "报名方式：@机器人 {keyword}\n奖项：\n{prizes}"
    ),
    "lottery_no_active": "本群当前没有未结束的抽奖活动。",
    "lottery_status": (
        "抽奖 #{activity_id}：{title}\n状态：{status}\n"
        "简介：{description}\n"
        "开始：{start_time}\n开奖：{draw_time}\n领奖截止：{claim_deadline}\n"
        "报名口令：{keyword}\n参与人数：{participant_count}\n奖项：\n{prizes}"
    ),
    "lottery_participants": (
        "抽奖参与者（第 {page} 页，共 {total} 人）：\n{participants}"
    ),
    "lottery_opened": "抽奖“{title}”现已开始，请发送：@机器人 {keyword}",
    "lottery_joined": "报名成功，请等待开奖。",
    "lottery_already_joined": "你已经参加过本场抽奖。",
    "lottery_not_open": "当前没有可报名或可领奖的抽奖活动。",
    "lottery_member_list_failed": "群成员列表读取失败，开奖已延迟，稍后会自动重试。",
    "lottery_no_winner": "抽奖“{title}”已开奖，但没有符合条件的参与者。",
    "lottery_drawn": (
        "抽奖“{title}”开奖结果：\n{winners}\n"
        "领奖截止：{claim_deadline}\n"
        "中奖者请发送：@机器人 抽奖 <New API用户ID>"
    ),
    "lottery_not_winner": "你不是当前抽奖的中奖者。",
    "lottery_claim_expired": "该中奖资格已经超过领奖期限。",
    "lottery_confirmation_exists": "你已有待确认的抽奖领奖申请。",
    "lottery_processing": "该中奖资格正在发放中，请勿重复操作。",
    "lottery_already_paid": "该中奖资格已经成功发放。",
    "lottery_manual_review_locked": "该中奖资格正在人工核查，暂时不能重复申请。",
    "lottery_confirmation": (
        "请确认抽奖领奖信息：\n活动：{activity}\n奖项：{prize}\n额度：{amount}\n"
        "New API ID：{user_id}\n用户名：{username}\n确认有效至：{expires_at}\n"
        "发送：@机器人 确认 抽奖\n或：@机器人 取消 抽奖"
    ),
    "lottery_confirmation_expired": "抽奖领奖确认已过期，请重新提交 New API 用户 ID。",
    "lottery_no_confirmation": "没有找到可确认的抽奖领奖申请。",
    "lottery_confirmation_cancelled": "已取消本次抽奖领奖确认，你可以在期限内重新提交。",
    "lottery_paid": (
        "抽奖奖励发放成功。活动：{activity}，额度：{amount}，"
        "New API ID：{user_id}，用户名：{username}，流水号：{serial}"
    ),
    "lottery_payout_failed": "抽奖奖励发放失败：{error}。资格已释放，可重新提交。",
    "lottery_manual_review": (
        "New API 返回结果不明确，已冻结中奖资格且不会自动重试。"
        "请管理员核查流水号：{serial}"
    ),
    "lottery_cancelled": ("抽奖 #{activity_id}“{title}”已取消。原因：{reason}"),
    "lottery_claim_closed": "抽奖“{title}”领奖时间已结束，未领取资格已作废。",
    "lottery_review_not_found": "没有找到需要核查的抽奖流水号。",
    "lottery_review_success": "抽奖流水 {serial} 已核查为成功并完成记账。",
    "lottery_review_failed": "抽奖流水 {serial} 已核查为失败并释放资格。",
    "comp_help": (
        "补偿管理指令：\n"
        "/补偿 开启 <每人额度> <持续时间|-> <总金额|-> [标题]\n"
        "/补偿 状态\n/补偿 记录 [页码]\n/补偿 关闭 [原因]\n"
        "/补偿 核查 <流水号> <成功|失败>"
    ),
    "comp_invalid_argument": "补偿指令参数错误，请发送 /补偿 帮助。",
    "comp_active_exists": "本群已有一场进行中的补偿活动。",
    "comp_budget_too_small": "补偿总金额不足以发放一份完整补偿。",
    "comp_opened": (
        "补偿活动已开启（#{activity_id}）：{title}\n每人：{amount}\n"
        "结束时间：{end_time}\n总预算：{total_budget}\n"
        "领取方式：@机器人 补偿 <New API用户ID>"
    ),
    "comp_no_active": "本群当前没有进行中的补偿活动。",
    "comp_status": (
        "补偿 #{activity_id}：{title}\n每人：{amount}\n结束时间：{end_time}\n"
        "总预算：{total_budget}\n剩余预算：{remaining_budget}\n记录数：{records}"
    ),
    "comp_records": "补偿记录（第 {page} 页，共 {total} 条）：\n{records}",
    "comp_ended": "本场补偿已经结束。",
    "comp_duplicate": "本场补偿中，该 QQ 或 New API ID 已领取或正在处理中。",
    "comp_confirmation": (
        "请确认补偿领取信息：\n活动：{activity}\n额度：{amount}\n"
        "New API ID：{user_id}\n用户名：{username}\n确认有效至：{expires_at}\n"
        "发送：@机器人 确认 补偿\n或：@机器人 取消 补偿"
    ),
    "comp_confirmation_expired": "补偿确认已过期，请重新提交 New API 用户 ID。",
    "comp_no_confirmation": "没有找到可确认的补偿申请。",
    "comp_confirmation_cancelled": "已取消本次补偿确认，你可以在活动结束前重新提交。",
    "comp_budget_insufficient": "剩余预算不足一份完整补偿，本场补偿已自动结束。",
    "comp_budget_reserved": ("剩余预算正被其他发放或人工核查冻结，请稍后再次确认。"),
    "comp_paid": (
        "补偿发放成功。活动：{activity}，额度：{amount}，"
        "New API ID：{user_id}，用户名：{username}，流水号：{serial}"
    ),
    "comp_payout_failed": "补偿发放失败：{error}。占用已释放，可重新提交。",
    "comp_manual_review": (
        "New API 返回结果不明确，已冻结本次资格和预算且不会自动重试。"
        "请管理员核查流水号：{serial}"
    ),
    "comp_closed": "补偿 #{activity_id}“{title}”已关闭。原因：{reason}",
    "comp_auto_closed": "补偿“{title}”已到结束时间，本场活动现已关闭。",
    "comp_review_not_found": "没有找到需要核查的补偿流水号。",
    "comp_review_success": "补偿流水 {serial} 已核查为成功并完成记账。",
    "comp_review_failed": "补偿流水 {serial} 已核查为失败并释放资格。",
}
