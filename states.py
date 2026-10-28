"""所有对话状态（FSM）。"""
from aiogram.fsm.state import State, StatesGroup


class SplitStates(StatesGroup):
    waiting_file = State()      # 等待上传 TXT
    waiting_lines = State()     # 等待每份行数
    waiting_filename = State()  # 等待输入文件名前缀（可选）
    waiting_insert = State()    # 等待选择是否插入手机号
    waiting_phones_count = State()  # 等待输入每份文件插入手机号数量
    waiting_phones = State()    # 等待发送手机号


class ZipStates(StatesGroup):
    waiting_file = State()      # 等待上传 ZIP


class RenameStates(StatesGroup):
    waiting_files = State()     # 等待上传多个 TXT
    waiting_name = State()      # 等待新文件名前缀


class VcfStates(StatesGroup):
    waiting_file = State()      # 等待号码文本/TXT


class DedupStates(StatesGroup):
    waiting_file = State()      # 等待上传多个 TXT / 文本


class MergeStates(StatesGroup):
    waiting_files = State()     # 等待上传多个 TXT
    waiting_filename = State()  # 等待输入导出文件名


class SortStates(StatesGroup):
    waiting_files = State()     # 等待上传多个 TXT
    waiting_order = State()     # 等待输入期望字段顺序


class XlsxStates(StatesGroup):
    waiting_files = State()     # 等待上传多个 XLSX


class ClassifyStates(StatesGroup):
    waiting_file = State()      # 等待发送号码文本/TXT
    waiting_choice = State()    # 等待选择分组方式
    waiting_filename = State()  # 等待输入文件名前缀（可选）


class PhoneCheckStates(StatesGroup):
    waiting_file = State()      # 等待发送号码文本/TXT


class ExtractStates(StatesGroup):
    waiting_file = State()      # 等待上传文件（txt/xlsx/csv/zip）
    waiting_dedup = State()     # 等待选择是否去重


class RedeemStates(StatesGroup):
    waiting_code = State()      # 等待输入卡密


class AdminStates(StatesGroup):
    gen_cards = State()         # 等待输入生成数量与天数
    delete_card = State()       # 等待输入要删除的卡密
    adjust_add = State()        # 等待输入 用户 + 添加天数
    adjust_deduct = State()     # 等待输入 用户 + 扣除天数
    broadcast = State()         # 等待输入广播内容
    edit_welcome = State()      # 等待输入新的欢迎语
    edit_plans = State()        # 等待输入新的套餐配置
    btn_icon_pick = State()     # 选择要设置图标的按钮
    btn_icon_input = State()    # 等待发会员表情
    bind_wash_chat = State()    # 等待绑定洗数据群
    edit_wash_prices = State()  # 等待输入洗数据定价
    set_price_floor = State()   # 等待输入洗数据价格下限
    set_wash_quota = State()    # 等待输入累计洗数据配额
    add_admin = State()         # 等待输入要添加的管理员ID
    remove_admin = State()      # 等待输入要删除的管理员ID
    wash_balance_input = State()  # 等待输入洗数据加扣余额
    alt_login_step1 = State()   # 小号登录：输入手机号
    alt_login_step2 = State()   # 小号登录：输入验证码
    alt_login_step3 = State()   # 小号登录：输入两步验证密码
    bind_drop_chat = State()    # 等待绑定掉落通知群
    edit_drop_template = State()  # 等待输入掉落通知模板
    lottery_config = State()    # 等待输入抽奖奖品配置
    lottery_premium_config = State()  # 等待输入高级抽奖奖品配置
    lottery_redeem_config = State()   # 等待输入兑换价目表
    lottery_template = State()  # 等待输入抽奖通知模板


class WashStates(StatesGroup):
    waiting_file = State()      # 等待用户上传要洗的文件
    waiting_custom_amount = State()  # 等待自定义充值金额


class AddAreaCodeStates(StatesGroup):
    waiting_file = State()      # 等待上传txt文件
