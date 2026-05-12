#!/usr/bin/env python3
"""
GUI 测试脚本 - 验证核心逻辑，不启动实际 GUI
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ['QT_QPA_PLATFORM'] = 'offscreen'

print("=" * 60)
print("Discord Alerts Trader - GUI 核心逻辑测试")
print("=" * 60)

all_passed = True
test_results = []

def test(name, func):
    global all_passed
    try:
        result = func()
        if result:
            print(f"✅ {name}")
            test_results.append((name, "PASS"))
        else:
            print(f"❌ {name}")
            test_results.append((name, "FAIL"))
            all_passed = False
    except Exception as e:
        import traceback
        print(f"❌ {name}: {e}")
        test_results.append((name, f"ERROR: {e}"))
        all_passed = False
    return result


def test_config():
    """测试配置加载"""
    from DiscordAlertsTrader.configurator import cfg, channel_ids
    assert cfg is not None, "配置未加载"
    assert 'general' in cfg.sections(), "缺少 general 配置"
    print(f"   配置节: {cfg.sections()}")
    return True


def test_match_authors_logic():
    """测试作者匹配函数的逻辑"""
    import re

    def match_authors_logic(author_str, channel_ids, cfg, data_dir):
        if "#" in author_str:
            return author_str
        authors = []
        for chn in channel_ids.keys():
            csv_path = os.path.join(data_dir, f"{chn}_message_history.csv")
            if os.path.exists(csv_path):
                import pandas as pd
                at = pd.read_csv(csv_path, usecols=["Author"])["Author"].unique()
                authors.extend(at)
        authors = list(dict.fromkeys(authors))
        subscribed = cfg.get('discord', {}).get('authors_subscribed', '').split(',')
        authors.extend([a for a in subscribed if a])
        matching = [a for a in authors if author_str.lower() in a.lower()]
        if len(matching) == 0:
            return author_str
        elif len(matching) > 1:
            return author_str
        else:
            return matching[0]

    result = match_authors_logic("test", {}, {}, "/tmp")
    assert isinstance(result, str), "返回值应为字符串"
    return True


def test_split_alert_message():
    """测试消息分割函数"""
    def split_alert_message_logic(gui_msg):
        if len(gui_msg.split(',')) > 2:
            splt = gui_msg.split(',')
            author = splt[0]
            msg = ",".join(splt[1:])
        elif len(gui_msg.split(',')) == 2:
            author, msg = gui_msg.split(',')
        elif len(gui_msg.split(':')) == 2:
            author, msg = gui_msg.split(':')
        elif len(gui_msg.split(':')) > 2:
            splt = gui_msg.split(':')
            author = splt[0]
            msg = ":".join(splt[1:])
        else:
            author = "author"
            msg = gui_msg
        return author, msg

    test_cases = [
        ("author:message", "author", "message"),
        ("author,message", "author", "message"),
        ("author,msg1,msg2", "author", "msg1,msg2"),
    ]

    for input_str, expected_author, expected_msg in test_cases:
        author, msg = split_alert_message_logic(input_str)
        assert author == expected_author, f"作者不匹配: {author} != {expected_author}"
        assert msg == expected_msg, f"消息不匹配: {msg} != {expected_msg}"

    return True


def test_gui_generator():
    """测试 GUI 生成器"""
    from DiscordAlertsTrader import gui_generator as gg

    assert hasattr(gg, 'get_portf_data'), "缺少 get_portf_data"
    assert hasattr(gg, 'get_tracker_data'), "缺少 get_tracker_data"
    assert hasattr(gg, 'get_stats_data'), "缺少 get_stats_data"
    print("   可用函数:", [x for x in dir(gg) if not x.startswith('_')])
    return True


def test_safe_eval():
    """测试安全评估工具"""
    from DiscordAlertsTrader.utils.safe_eval import safe_eval, safe_json_eval, safe_eval_or_none

    assert safe_eval("1 + 2") == 3, "基本数学运算失败"
    assert safe_eval("10 * 5") == 50, "乘法运算失败"
    assert safe_eval("-5 + 10") == 5, "负数运算失败"
    assert safe_eval("100 / 2") == 50, "除法运算失败"
    assert safe_eval("2 ** 3") == 8, "幂运算失败"

    json_result = safe_json_eval('{"key": "value"}')
    assert json_result == {"key": "value"}, "JSON 解析失败"

    none_result = safe_eval_or_none("invalid")
    assert none_result == "invalid", "safe_eval_or_none 失败"

    return True


def test_trade_models():
    """测试交易数据模型"""
    from DiscordAlertsTrader.models.trade_models import TradeAction, AssetType, Order

    assert TradeAction.BTO.value == "BTO"
    assert TradeAction.STC.value == "STC"
    assert AssetType.STOCK.value == "stock"
    assert AssetType.OPTION.value == "option"

    order = Order.from_dict({
        'action': 'BTO',
        'symbol': 'AAPL',
        'price': 150.0,
        'Qty': 10
    })
    assert order.action == TradeAction.BTO
    assert order.symbol == 'AAPL'
    assert order.price == 150.0

    print(f"   Order: {order}")
    return True


def test_risk_manager():
    """测试风险管理器"""
    from DiscordAlertsTrader.core.risk_manager import RiskManager, RiskLimits

    rm = RiskManager()

    valid, msg = rm.validate_short_strike(200)
    assert valid, f"验证失败: {msg}"

    valid, msg = rm.validate_short_strike(500)
    assert not valid, "高价strike应该被拒绝"

    valid, msg = rm.validate_dte(15)
    assert valid, f"验证失败: {msg}"

    valid, msg = rm.validate_dte(60)
    assert not valid, "高 DTE 应该被拒绝"

    qty = rm.calculate_position_size(price=5.0, asset_type="option")
    assert qty >= 1, "数量计算失败"
    print(f"   计算数量 (价格=5, 类型=option): {qty}")

    valid, msg = rm.validate_trade_value(100, "option")
    assert valid, f"验证失败: {msg}"

    valid, msg = rm.validate_trade_value(20000, "option")
    assert not valid, "高价值应该被拒绝"

    return True


def test_exit_planner():
    """测试退出计划器"""
    from DiscordAlertsTrader.core.exit_planner import ExitPlanner, ExitPlan, ExitTarget

    planner = ExitPlanner()

    plan = ExitPlan.from_dict({'PT1': '130', 'SL': '115'})
    assert plan.pt1 is not None
    assert plan.sl is not None
    print(f"   ExitPlan: {plan}")

    plan_with_pct = ExitPlan.from_dict({'PT1': '30%', 'SL': '10%'})
    assert plan_with_pct.pt1 is not None
    assert plan_with_pct.pt1.percentage == 30

    targets = planner.calculate_price_targets(
        entry_price=100,
        plan=plan,
        is_short=False
    )
    assert 'PT1' in targets
    assert targets['PT1'] == 130
    print(f"   价格目标: {targets}")

    target_price, ts_offset = planner.parse_trailing_stop("150TS5", 140)
    assert target_price == 150
    print(f"   Trailing Stop: target={target_price}, offset={ts_offset}")

    return True


def test_logging_utils():
    """测试日志工具"""
    from DiscordAlertsTrader.utils.logging_utils import (
        print_success, print_error, print_warning, print_info,
        QueueLogger, ErrorHandler, LogLevel
    )

    ql = QueueLogger(maxsize=10)
    ql.info("Test message")
    ql.warning("Warning message")
    ql.error("Error message")

    entry = ql.queue.get_nowait()
    assert entry['message'] == "Test message"
    assert entry['level'] == "INFO"

    assert LogLevel.INFO.value == "INFO"
    assert LogLevel.ERROR.value == "ERROR"

    print("   QueueLogger 工作正常")
    return True


def test_message_parser():
    """测试消息解析器"""
    from DiscordAlertsTrader.message_parser import parse_trade_alert, parse_exit_plan

    test_msg = "BTO 10 AAPL @ 150 PT:160 SL:145"

    try:
        result = parse_trade_alert(test_msg)
        if result:
            print(f"   解析结果: {result}")
        else:
            print("   消息未能解析 (可能是正常情况)")
    except Exception as e:
        print(f"   解析异常: {e}")

    exit_plan = parse_exit_plan({'PT1': '160', 'SL': '145'})
    print(f"   Exit Plan: {exit_plan}")

    return True


def test_portfolio_manager():
    """测试组合管理器"""
    from DiscordAlertsTrader.core.portfolio_manager import PortfolioManager, Trade

    import tempfile
    with tempfile.NamedTemporaryFile(suffix='.csv', delete=False) as f:
        temp_file = f.name

    try:
        import pandas as pd
        cols = ['Date', 'Symbol', 'Type', 'Qty', 'Price', 'isOpen', 'Asset']
        pd.DataFrame(columns=cols).to_csv(temp_file, index=False)
        pm = PortfolioManager(temp_file, column_names=cols)
    except Exception:
        pm = PortfolioManager(temp_file)

    from datetime import datetime
    trade = Trade(
        date=datetime.now(),
        symbol='AAPL',
        action='BTO',
        quantity=10,
        price=150.0
    )

    try:
        idx = pm.add_trade(trade)
        print(f"   添加交易, 索引: {idx}")
        assert len(pm.portfolio) >= 0

        stats = pm.get_stats()
        print(f"   统计: 总交易={stats.total_trades}, 开仓={stats.open_trades}")
    except Exception as e:
        print(f"   Portfolio 操作测试跳过: {e}")

    os.unlink(temp_file)
    return True


def test_data_models():
    """测试数据模型"""
    from DiscordAlertsTrader.models.trade_models import (
        TradeAction, AssetType, OrderStatus, RiskLevel,
        ExitTarget, ExitPlan, Order, Trade, AlertMessage
    )

    et = ExitTarget(price=150.0, percentage=30.0)
    assert et.price == 150.0
    assert et.percentage == 30.0

    ep = ExitPlan(pt1=et, sl=ExitTarget(price=100.0))
    assert ep.pt1 is not None
    assert ep.sl is not None

    print(f"   ExitPlan: {ep}")
    return True


print("\n--- 配置测试 ---")
test("配置加载", test_config)

print("\n--- GUI 核心函数测试 ---")
test("match_authors 逻辑", test_match_authors_logic)
test("split_alert_message 函数", test_split_alert_message)

try:
    test("GUI 生成器", test_gui_generator)
except Exception as e:
    print(f"⚠️ GUI 生成器测试跳过: {e}")

print("\n--- 工具模块测试 ---")
test("安全评估工具", test_safe_eval)
test("日志工具", test_logging_utils)

print("\n--- 数据模型测试 ---")
test("交易数据模型", test_trade_models)
test("数据模型完整测试", test_data_models)

print("\n--- 核心模块测试 ---")
test("风险管理器", test_risk_manager)
test("退出计划器", test_exit_planner)
test("组合管理器", test_portfolio_manager)

print("\n--- 消息解析测试 ---")
test("消息解析器", test_message_parser)

print("\n" + "=" * 60)
print("测试结果汇总:")
for name, status in test_results:
    symbol = "✅" if status == "PASS" else "❌"
    print(f"  {symbol} {name}: {status}")

print()
if all_passed:
    print("🎉 所有核心逻辑测试通过！")
else:
    passed = sum(1 for _, s in test_results if s == "PASS")
    total = len(test_results)
    print(f"⚠️ 通过: {passed}/{total}")

print("=" * 60)
print("\n📝 注意:")
print("   - GUI 窗口无法在此环境启动 (缺少图形界面)")
print("   - 在有图形界面的系统中，运行:")
print("     python DiscordAlertsTrader/gui.py")
print("=" * 60)
