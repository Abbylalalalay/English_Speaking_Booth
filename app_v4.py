import streamlit as st
import requests
from bs4 import BeautifulSoup
import time
import os
import subprocess
import tempfile
from streamlit_mic_recorder import mic_recorder
import google.generativeai as genai
import genanki
import io  # 用于处理文件下载
import re  # 用于把文章切割成一句句
import base64
import os
import re
import json
import time
import random
import tempfile
from datetime import datetime
import requests
from bs4 import BeautifulSoup
import glob  # 专门用来找文件

import streamlit as st
import google.generativeai as genai
from streamlit_mic_recorder import mic_recorder
from supabase import create_client, Client


# 初始化 Supabase 数据库连接
@st.cache_resource
def init_connection():
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_KEY"]
    return create_client(url, key)


supabase = init_connection()

# ==========================================
# 🧠 核心记忆引擎与 Sivers 爬虫
# ==========================================
HISTORY_FILE = "learning_history.json"


# 1. 自动抓取 Sivers 博客的所有文章链接 (加缓存，每天只爬一次目录)
@st.cache_data(ttl=86400)
def get_all_sivers_links():
    try:
        url = "https://sive.rs/blog"
        # 💡 穿上隐身衣：伪装成真实的 Mac/Chrome 浏览器
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        # 把超时时间拉长到 15 秒，给代理一点反应时间
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()

        soup = BeautifulSoup(r.text, "html.parser")
        links = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            # 精准过滤
            if (
                href.startswith("/")
                and len(href) > 2
                and not href.startswith(
                    ("/blog", "/contact", "/about", "/projects", "/book")
                )
            ):
                links.append("https://sive.rs" + href)
        return list(set(links))

    except Exception as e:
        # 💡 不要静默失败，把真实的底层网络报错直接打在屏幕上！
        st.error(f"🚨 底层网络报错抓取失败: {e}")
        return []


# ==========================================
# 🕷️ 补充缺失的 Sivers 单篇文章抓取器
# ==========================================
def fetch_sivers_article(url):
    """
    负责进入具体的文章页面，扒出标题、正文和原声音频链接
    """
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # 1. 抓取标题 (💡 放弃不可靠的 h1，改用浏览器顶部的 title 标签并进行清洗)
        title_tag = soup.find("title")
        if title_tag:
            # 举例：把 "How to Live | Derek Sivers" 按照 "|" 切开
            # [0] 拿走左边的 "How to Live "，并用 strip() 洗掉两端的空格
            title = title_tag.text.split("|")[0].strip()
        else:
            title = "Sivers Article"

        # 2. 抓取正文 (Sivers 的网页极其干净，直接抓 <article> 或所有的 <p> 标签)
        article_body = soup.find("article")
        if article_body:
            paragraphs = article_body.find_all("p")
        else:
            paragraphs = soup.find_all("p")

        # 过滤掉极短的无用段落，然后用双换行符拼接起来
        text = "\n\n".join(
            [p.text.strip() for p in paragraphs if len(p.text.strip()) > 10]
        )

        # 3. 抓取原声音频链接 (通常在 <audio> 标签里)
        audio_url = ""
        audio_tag = soup.find("audio")
        if audio_tag:
            source = audio_tag.find("source")
            if source and source.has_attr("src"):
                audio_url = source["src"]
            elif audio_tag.has_attr("src"):
                audio_url = audio_tag["src"]

        # Sivers 有时候用相对路径（比如 /audio/relax.mp3），帮它补全前面的域名
        if audio_url and audio_url.startswith("/"):
            audio_url = "https://sive.rs" + audio_url

        return title, text, audio_url

    except Exception as e:
        st.error(f"抓取文章内容失败，可能是网络波动: {e}")
        return "", "", ""


# 2. 读写学习记录 (连同文章原文一起存下，方便复习)
def load_history():
    """从 Supabase 云端数据库读取历史记录"""
    try:
        response = supabase.table("history").select("*").execute()
        history_dict = {}
        # 将数据库里的列表数据，转换回我们之前熟悉的字典格式
        for row in response.data:
            history_dict[row["id"]] = {
                "title": row.get("title", ""),
                "date": row.get("date", ""),
                "text": row.get("text", ""),
                "audio_url": row.get("audio_url", ""),
                "needs_review": row.get("needs_review", False),
            }
        return history_dict
    except Exception as e:
        st.error(f"读取云端数据库失败: {e}")
        return {}


def save_history(history_dict):
    """将历史记录保存（Upsert）到 Supabase 云端数据库"""
    data_to_upsert = []
    # 把字典重新转换成数据库认识的列表格式
    for aid, info in history_dict.items():
        data_to_upsert.append(
            {
                "id": aid,  # 这个是我们之前生成的唯一 URL 或 manual_id
                "title": info.get("title", ""),
                "date": info.get("date", ""),
                "text": info.get("text", ""),
                "audio_url": info.get("audio_url", ""),
                "needs_review": info.get("needs_review", False),
            }
        )

    try:
        # upsert 极其强大：如果 id 不存在就插入新数据，如果存在就更新覆盖
        supabase.table("history").upsert(data_to_upsert).execute()
    except Exception as e:
        st.error(f"同步云端数据库失败: {e}")


# ==========================================
# 💡 新增：从 Supabase 彻底删除单条历史记录的函数
# ==========================================
def delete_history_item(article_id):
    try:
        # 告诉云端数据库：删掉 id 等于当前 article_id 的那行数据
        supabase.table("history").delete().eq("id", article_id).execute()
    except Exception as e:
        st.error(f"删除云端记录失败: {e}")


# 3. 清理状态缓存的辅助函数（切换文章时必须清理旧数据）
def clear_training_states():
    keys_to_delete = []
    for key in st.session_state.keys():
        if key.startswith(("s1_", "s2_", "s4_", "s6_", "s7_", "vocab_", "demo_")):
            keys_to_delete.append(key)
    for key in keys_to_delete:
        del st.session_state[key]

    # 💡 强行刷新所有容易卡死的组件的随机种子
    st.session_state["component_refresh_seed"] = str(time.time()).replace(".", "")

    # ==========================================
    # 🧹 自动垃圾回收机制 (静默清理硬盘旧音频)
    # ==========================================
    # 寻找当前文件夹下所有 AI 生成的音频文件
    audio_files_to_delete = (
        glob.glob("demo_*.mp3")  # Step 4 逐句示范音
        + glob.glob("chat_*.mp3")  # Step 7 聊天回复音
        + glob.glob("main_fallback*.mp3")  # 兜底的主音频
    )

    for file_path in audio_files_to_delete:
        try:
            os.remove(file_path)
        except Exception:
            # 如果文件刚好被占用或不存在，直接跳过，绝不报错中断程序
            pass


# 4. 切换文章的“需要复习”状态 (Callback 回调函数)
def toggle_review(article_id):
    hist = load_history()
    # 获取当前状态，如果没有这个标签（比如之前的旧数据），默认为 False
    current_status = hist[article_id].get("needs_review", False)
    # 状态翻转（True 变 False，False 变 True）
    hist[article_id]["needs_review"] = not current_status
    save_history(hist)


# ==========================================

st.set_page_config(page_title="Native 英语特训舱 V1.0", page_icon="🚀", layout="wide")
st.title("🎧 Native 英语特训舱 V1.0")

# ==========================================
# 🎮 侧边栏：获取内容与历史记录
# ==========================================
history = load_history()

with st.sidebar:
    # --- 0. 配置 AI 大脑 (置顶，体验更好) ---
    st.header("⚙️ 系统设置")

    # 💡 终极混合驱动：先尝试从云端隐秘保险柜拿 Key
    if "GEMINI_API_KEY" in st.secrets:
        api_key = st.secrets["GEMINI_API_KEY"]
        st.success("🟢 云端专属 AI 大脑已自动连接！")
    else:
        # 💡 核心记忆锁：加上 key="saved_api_key"，死死锁住输入框的内容
        api_key = st.text_input(
            "🔑 输入 Gemini API Key:", type="password", key="saved_api_key"
        )
        if api_key:
            st.success("🟢 本地 AI 大脑已连接！")
        else:
            st.warning("⚠️ 请先输入 API Key 激活 AI 功能。")

    # 只要拿到了 Key，就激活大模型
    if api_key:
        genai.configure(api_key=api_key)
        # 💡 修复模型名称为正确的版本
        model = genai.GenerativeModel("gemini-3.1-flash-lite-preview")

    st.markdown("---")

    # --- 1. 学习控制台 ---
    st.header("🎯 学习控制台")

    # 创建两个选项卡
    tab_sivers, tab_manual = st.tabs(["🎲 Sivers 盲盒", "✍️ 手动添加"])

    with tab_sivers:
        st.markdown("从 sive.rs 自动抽取一篇你没看过的原文和原声音频。")
        all_links = get_all_sivers_links()
        practiced_ids = list(history.keys())
        unpracticed_links = [url for url in all_links if url not in practiced_ids]

        if all_links:
            st.caption(
                f"全站总计: {len(all_links)} 篇 | 你的待解锁: {len(unpracticed_links)} 篇"
            )
            if unpracticed_links:
                if st.button("🎲 抽取今日新文章 (优先原声)", use_container_width=True):
                    with st.spinner("正在满世界寻找带有 Native 原声的优质文章..."):
                        found_article = None
                        fallback_article = None

                        # 💡 核心机制：最多进行 5 次静默连抽
                        max_attempts = 5
                        available_links = unpracticed_links.copy()  # 复制一份用来抽卡

                        for _ in range(max_attempts):
                            if not available_links:
                                break

                            chosen_url = random.choice(available_links)
                            available_links.remove(chosen_url)  # 抽过的踢出去，避免重复

                            title, text, audio_url = fetch_sivers_article(chosen_url)

                            if text:
                                if audio_url:
                                    # 🎉 中大奖了！找到了带原声的文章，立刻停止抽卡！
                                    found_article = (chosen_url, title, text, audio_url)
                                    break
                                else:
                                    # 没有原声，先委屈一下当备胎（存下第一篇有效的即可）
                                    if not fallback_article:
                                        fallback_article = (
                                            chosen_url,
                                            title,
                                            text,
                                            audio_url,
                                        )

                        # 💡 最终决断：优先用原声的，实在没有就用备胎
                        final_article = (
                            found_article if found_article else fallback_article
                        )

                        if final_article:
                            final_url, title, text, audio_url = final_article

                            # 装载进系统
                            st.session_state["text"] = text
                            st.session_state["title"] = title
                            st.session_state["audio_url"] = audio_url
                            st.session_state["current_id"] = final_url

                            # 存入历史记录
                            history[final_url] = {
                                "title": title,
                                "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
                                "text": text,
                                "audio_url": audio_url,
                            }
                            save_history(history)
                            clear_training_states()
                            st.rerun()
                        else:
                            st.error("抓取失败，请稍后重试。")
            else:
                st.success("🏆 神级成就！Sivers 的博客全库已被你刷穿！")
        else:
            st.error("网络不畅，获取目录失败。")

    with tab_manual:
        st.markdown("练习你看外刊或开会时的段落。")
        manual_text = st.text_area("粘贴你要练习的英文原文:", height=150)
        manual_title = st.text_input("给这篇素材起个标题 (选填):", "自定义文章")

        if st.button("🚀 开始特训", use_container_width=True):
            if manual_text.strip():
                # 生成唯一 ID
                art_id = f"manual_{datetime.now().strftime('%Y%m%d%H%M%S')}"

                st.session_state["text"] = manual_text
                st.session_state["title"] = manual_title
                st.session_state["audio_url"] = (
                    ""  # 手动输入无原声，后续会自动触发 TTS 兜底
                )
                st.session_state["current_id"] = art_id

                history[art_id] = {
                    "title": manual_title,
                    "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "text": manual_text,
                    "audio_url": "",
                }
                save_history(history)
                clear_training_states()
                st.rerun()
            else:
                st.warning("请先粘贴文本哦！")

    st.markdown("---")

    # --- 2. 复习区 ---
    st.subheader("📚 历史复习库")
    if not history:
        st.caption("空空如也，赶紧开始你的第一篇吧！")
    else:
        # 倒序排列，今天刚练的在最上面
        for aid, info in reversed(list(history.items())):
            needs_review = info.get("needs_review", False)
            icon = "⭐" if needs_review else "✅"

            with st.expander(f"{icon} {info['date'][:10]} | {info['title'][:15]}"):

                st.checkbox(
                    "⭐ 标为需要重点复习",
                    value=needs_review,
                    key=f"mark_{aid}",
                    on_change=toggle_review,
                    args=(aid,),
                )

                # 💡 核心修改：使用列布局，把“重新挑战”和“删除”并排放在一起
                col_retry, col_del = st.columns([4, 1])

                with col_retry:
                    if st.button(
                        "🔄 重新挑战", key=f"retry_{aid}", use_container_width=True
                    ):
                        st.session_state["text"] = info["text"]
                        st.session_state["title"] = info["title"]
                        st.session_state["audio_url"] = info.get("audio_url", "")
                        st.session_state["current_id"] = aid
                        clear_training_states()
                        st.rerun()

                with col_del:
                    # 💡 新增的删除按钮
                    if st.button("🗑️", key=f"del_hist_{aid}", help="永久删除此记录"):
                        # 1. 呼叫刚刚写的函数，清理云端数据
                        delete_history_item(aid)
                        # 2. 强制页面刷新，系统会自动重新拉取最新的数据库，这篇文章就瞬间消失了！
                        st.rerun()


# --- 1. 核心逻辑：网页抓取与兜底音频 ---
def fetch_sivers_article(url):
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        title_tag = soup.find("h1")
        title = title_tag.text.strip() if title_tag else "未找到标题"

        paragraphs = soup.find_all("p")
        text_content = "\n\n".join(
            [p.text.strip() for p in paragraphs if p.text.strip() != ""]
        )

        audio_url = None
        audio_tag = soup.find("audio")
        if audio_tag and audio_tag.has_attr("src"):
            audio_url = audio_tag["src"]
            if not audio_url.startswith("http"):
                audio_url = f"https://sive.rs{audio_url}"

        return title, text_content, audio_url
    except Exception as e:
        return "抓取失败", "", None


def generate_fallback_audio(text, filename="fallback_audio.mp3"):
    with open("temp_text.txt", "w", encoding="utf-8") as f:
        f.write(text)
    subprocess.run(
        [
            "edge-tts",
            "-v",
            "en-US-GuyNeural",
            "-f",
            "temp_text.txt",
            "--write-media",
            filename,
        ]
    )
    return filename


# --- 3. 核心训练区 (当语料存在时才显示) ---
if "text" in st.session_state and st.session_state["text"]:

    if "chat_history" not in st.session_state:
        st.session_state["chat_history"] = []
        st.session_state["q_count"] = 1
        st.session_state["current_q"] = (
            "What is the main idea the author is trying to convey in this article?"
        )

    if "s2_chat_history" not in st.session_state:
        st.session_state["s2_reading_completed"] = False
        st.session_state["s2_chat_history"] = []
        st.session_state["s2_q_count"] = 1
        st.session_state["s2_current_q"] = (
            "Now that you've read the text, what specific details did you notice that you completely missed during the listening phase?"
        )

        # 💡 核心修复：不管有没有网络原声，先给 local_audio 占个位，防止 KeyError 报错！
        if "local_audio" not in st.session_state:
            st.session_state["local_audio"] = None

        # 如果没有网络原声（说明是手动输入的），并且本地还没生成过，立刻生成本地 AI 语音兜底
        if not st.session_state.get("audio_url") and not st.session_state.get(
            "local_audio"
        ):
            with st.spinner("正在生成高保真 AI 语音，请稍候..."):
                st.session_state["local_audio"] = generate_fallback_audio(
                    st.session_state["text"], "main_fallback.mp3"
                )

    st.markdown("---")
    st.header(f"📄 {st.session_state['title']}")

    # == Step 1: 盲听与 AI 连续问答 ==
    st.subheader("Step 1: 盲听与连续问答训练 (5 轮挑战)")

    # 播放音频
    if st.session_state["local_audio"]:
        st.audio(st.session_state["local_audio"])
    elif st.session_state["audio_url"]:
        st.audio(st.session_state["audio_url"])

    # 1. 渲染历史对话记录 (使用 Streamlit 原生的漂亮气泡组件)
    for chat in st.session_state["chat_history"]:
        with st.chat_message(chat["role"]):
            st.markdown(chat["content"])

    # 2. 如果还没问完 5 个问题，继续提问与录音
    if st.session_state["q_count"] <= 5:
        st.info(
            f"**📍 当前进度：Question {st.session_state['q_count']} / 5**\n\n**🤖 AI 老师提问：** {st.session_state['current_q']}"
        )

        audio_info = mic_recorder(
            start_prompt="▶️ 点击开始录音",
            stop_prompt="⏹️ 我说完了，点击停止",
            key=f"recorder_{st.session_state['q_count']}",
        )

        if audio_info and api_key:
            audio_bytes = audio_info["bytes"]

            # 增加一个播放器，让你在提交前能自己听一下录得完不完整
            st.audio(audio_bytes, format="audio/wav")

            if st.button(
                "🚀 确认无误，提交回答", key=f"btn_{st.session_state['q_count']}"
            ):
                with st.spinner("AI 老师正在批改并构思下一个问题..."):
                    try:
                        with tempfile.NamedTemporaryFile(
                            delete=False, suffix=".wav"
                        ) as temp_audio:
                            temp_audio.write(audio_bytes)
                            temp_audio_path = (
                                temp_audio.name
                            )  # 👈 修复了变量未定义的问题

                        audio_file = genai.upload_file(path=temp_audio_path)

                        # 💡 核心：强制 AI 给出评价后，立刻生成下一个问题
                        prompt = f"""
                        你是一个非常严格且专业的美国 Native 英语外教。
                        今天学生听的文章原文是：“{st.session_state['text']}”
                        
                        当前任务：
                        刚才你问了学生第 {st.session_state['q_count']} 个问题："{st.session_state['current_q']}"
                        请听附件中学生的语音回答。
                        
                        请严格按照以下格式输出（每个部分之间必须保留一个空行）：
                        【你的原话】: (精准转录学生的语音)
                        【纠错与评价】: (结合文章评价理解是否准确，并指出语法或用词错误，并纠正)
                        【Native 优化】: (直接给出纯正地道英文口语表达的答案，尽量精简不要废话。绝对不要包含废话，只输出英文。绝对不要使用 "#"、"===" 等任何可能放大字体的符号)
                        【Next Question】: (提出一个新问题。直接写出英文句子。如果是第5题，写一句总结语)
                        """

                        response = model.generate_content([prompt, audio_file])
                        reply_text = response.text

                        # --- 强制排版引擎与容错逻辑 ---
                        # 1. 粗暴清洗：删掉加粗星号、标题井号、下划线等所有会干扰排版的特殊符号，统一冒号
                        clean_reply = (
                            reply_text.replace("**", "")
                            .replace("#", "")
                            .replace("===", "")
                            .replace("---", "")
                            .replace("：", ":")
                        )

                        # 2. 分离问题与反馈
                        if "【Next Question】" in clean_reply:
                            parts = clean_reply.split("【Next Question】", 1)
                            feedback = parts[0].strip()
                            next_q = parts[1].strip(" :\n")
                        else:
                            feedback = clean_reply
                            next_q = "Could you tell me a bit more about your thoughts on this part?"

                        # 3. 强制换行排版：给每个标签前后加上明确的双回车和加粗，绝对保证分段清晰！
                        feedback = feedback.replace(
                            "【你的原话】", "\n\n**【你的原话】**\n"
                        )
                        feedback = feedback.replace(
                            "【纠错与评价】", "\n\n**【纠错与评价】**\n"
                        )
                        feedback = feedback.replace(
                            "【Native 优化】", "\n\n**【Native 优化】**\n"
                        )

                        # 清除可能产生的开头多余空行
                        feedback = feedback.strip()

                        # 将记录保存到记忆库
                        st.session_state["chat_history"].append(
                            {"role": "user", "content": f"*(提交了语音回答)*"}
                        )
                        st.session_state["chat_history"].append(
                            {"role": "assistant", "content": feedback.strip()}
                        )

                        # 更新状态，准备进入下一题
                        st.session_state["current_q"] = next_q.strip()
                        st.session_state["q_count"] += 1

                        os.remove(temp_audio_path)

                        # 强制刷新页面，显示最新气泡和新问题
                        st.rerun()

                    except Exception as e:
                        st.error(f"网络或接口错误: {e}")
    else:
        if not st.session_state.get("s1_has_fired_balloons", False):
            st.balloons()
            st.session_state["s1_has_fired_balloons"] = True  # 撒花庆祝
        st.success(
            "🎉 太棒了！你已经完成了 5 轮高强度的盲听问答挑战！请向下进入限时阅读环节。"
        )

    # == Step 2: 限时阅读与深度问答 ==
    st.subheader("Step 2: 限时阅读与深度问答 (2 轮挑战)")

    # 如果还没完成阅读，显示阅读界面
    if not st.session_state["s2_reading_completed"]:
        # 1. 自动计算阅读时间 (假设非母语者精读速度为 180词/分钟)
        word_count = len(st.session_state["text"].split())
        suggested_time = max(30, int((word_count / 180) * 60))

        st.info(
            f"📊 本文共约 **{word_count}** 词。系统建议精读时间：**{suggested_time}** 秒。"
        )
        read_time = st.slider("你可以微调阅读时间 (秒):", 10, 300, suggested_time, 5)

        if st.button("📖 开始限时挑战 (时间到后文本将永久隐藏)"):
            text_placeholder = st.empty()
            progress_bar = st.progress(100)

            # 💡 核心修改：弃用 Markdown 引用，改用安全的纯 HTML 渲染，并加入内部滚动条
            safe_text = st.session_state["text"].replace("\n", "<br>")
            html_content = f"""
                    <div style="
                        font-size: 16px; 
                        line-height: 1.8; 
                        padding: 15px; 
                        background-color: #f8f9fa; 
                        border-radius: 8px; 
                        border-left: 4px solid #4CAF50; 
                        color: #1f2937; 
                        font-family: sans-serif;
                        height: 350px; 
                        overflow-y: auto; 
                        margin-bottom: 10px;
                    ">
                        {safe_text}
                    </div>
                    """
            text_placeholder.markdown(html_content, unsafe_allow_html=True)

            # 倒计时循环
            for i in range(read_time, 0, -1):
                progress_bar.progress(i / read_time)
                time.sleep(1)

            # 时间到，清空占位符，更新状态并强制刷新页面
            text_placeholder.empty()
            progress_bar.empty()
            st.session_state["s2_reading_completed"] = True
            st.rerun()  # 👈 关键：刷新页面，进入问答环节
    # 如果已经读完了，显示深层问答界面
    else:
        st.error("⏱️ 时间到！文本已折叠。请依靠短期记忆回答以下细节问题。")

        # 渲染 Step 2 历史记录
        for chat in st.session_state["s2_chat_history"]:
            with st.chat_message(chat["role"]):
                st.markdown(chat["content"])

        # 2 轮连问逻辑
        if st.session_state["s2_q_count"] <= 2:
            st.info(
                f"**📍 深度拷问：Question {st.session_state['s2_q_count']} / 2**\n\n**🤖 AI 老师提问：** {st.session_state['s2_current_q']}"
            )

            audio_info = mic_recorder(
                start_prompt="▶️ 点击开始录音",
                stop_prompt="⏹️ 我说完了，点击停止",
                key=f"s2_recorder_{st.session_state['s2_q_count']}",
            )

            if audio_info and api_key:
                audio_bytes = audio_info["bytes"]
                st.audio(audio_bytes, format="audio/wav")

                if st.button(
                    "🚀 提交阅读理解", key=f"s2_btn_{st.session_state['s2_q_count']}"
                ):
                    with st.spinner("AI 老师正在核对文章细节..."):
                        try:
                            with tempfile.NamedTemporaryFile(
                                delete=False, suffix=".wav"
                            ) as temp_audio:
                                temp_audio.write(audio_bytes)
                                temp_audio_path = temp_audio.name

                            audio_file = genai.upload_file(path=temp_audio_path)

                            # 💡 Step 2 专属 Prompt：要求深挖细节，且不重复听力阶段的问题
                            prompt = f"""
                            你是一个非常严格且专业的美国 Native 英语外教。
                            现在是【阅读后深度问答阶段】。文章原文是：“{st.session_state['text']}”
                            
                            之前在听力阶段，你已经问过关于核心大意的问题了。
                            当前任务：
                            刚才你问了学生第 {st.session_state['s2_q_count']} 个阅读细节问题："{st.session_state['s2_current_q']}"
                            请听附件中学生的语音回答。
                            
                            请严格按照以下格式输出（每个部分之间必须保留一个空行）：
                            【你的原话】: (精准转录学生的语音)
                            【纠错与评价】: (结合文章【细节和逻辑】评价理解是否准确，并指出语法和用词错误并纠正)
                            【Native 优化】: (对于刚才问出的阅读细节问题："{st.session_state['s2_current_q']}的，直接给出纯正地道英文口语表达的答案，尽量准确精简。绝对不要包含废话，绝对不要使用 "#" 等符号)
                            【Next Question】: (如果当前不是第2题，请基于文章更深层的逻辑、生僻词或作者的潜在意图，提出一个非常具体的、深度挖掘的新英文问题。绝对不要问全篇大意。如果是第2题，写一句赞美的话)
                            """

                            response = model.generate_content([prompt, audio_file])
                            reply_text = (
                                response.text.replace("**", "")
                                .replace("#", "")
                                .replace("===", "")
                                .replace("---", "")
                                .replace("：", ":")
                            )

                            if "【Next Question】" in reply_text:
                                parts = reply_text.split("【Next Question】", 1)
                                feedback = parts[0].strip()
                                next_q = parts[1].strip(" :\n")
                            else:
                                feedback = reply_text
                                next_q = "Could you explain the author's logic behind that specific point?"

                            # 排版引擎
                            feedback = feedback.replace(
                                "【你的原话】", "\n\n**【你的原话】**\n"
                            )
                            feedback = feedback.replace(
                                "【纠错与评价】", "\n\n**【纠错与评价】**\n"
                            )
                            feedback = feedback.replace(
                                "【Native 优化】", "\n\n**【Native 优化】**\n"
                            ).strip()

                            st.session_state["s2_chat_history"].append(
                                {"role": "user", "content": f"*(提交了语音回答)*"}
                            )
                            st.session_state["s2_chat_history"].append(
                                {"role": "assistant", "content": feedback}
                            )

                            st.session_state["s2_current_q"] = next_q
                            st.session_state["s2_q_count"] += 1

                            os.remove(temp_audio_path)
                            st.rerun()

                        except Exception as e:
                            error_msg = str(e)
                            # 如果是 429 频率限制错误
                            if "429" in error_msg or "Quota exceeded" in error_msg:
                                st.warning(
                                    "⏳ 你的学习热情太高涨，触发了 AI 的频率保护机制！程序正在自动为你等待 30 秒后重试..."
                                )
                                time.sleep(35)  # 强制代码暂停 35 秒
                                st.rerun()  # 35秒后自动刷新页面重试
                            else:
                                st.error(f"网络或接口错误: {e}")
        else:
            # 💡 核心修复：用 session_state 记住是否已经放过气球了
            if not st.session_state.get("has_fired_balloons", False):
                st.balloons()
                st.session_state["has_fired_balloons"] = (
                    True  # 放完立刻上锁，绝不放第二次！
                )
            st.success(
                "🎉 太棒了！你已经完成了深度的限时阅读与细节审问！你对文章的理解已经达到了 Native 水平。"
            )

    st.markdown("---")
    # == Step 3: 查词与 Anki 自动制卡 ==
    st.subheader("Step 3: 核心词汇打捞与 Anki 制卡")

    # 1. 初始化词汇本记忆库
    if "vocab_list" not in st.session_state:
        st.session_state["vocab_list"] = []

    st.info(
        "💡 在阅读中遇到了生词或绝妙的短语？复制并粘贴到下方，AI 将自动为你提取原句上下文并生成记忆卡片。"
    )

    # 为了方便对照，再次用滚动框展示原文
    st.markdown("👇 **文章原文对照区：**")
    # 创建一个高度固定且可滚动的容器
    # 1. 展示全文 (强制 HTML 渲染，彻底屏蔽 Markdown 干扰)
    with st.container(height=300):
        # 把原文里的回车符替换成 HTML 的换行标签
        safe_text = st.session_state["text"].replace("\n", "<br>")

        # 用纯 HTML 强制锁定字体大小、颜色和排版边界
        st.markdown(
            f"""
        <div style="
            font-size: 16px; 
            line-height: 1.8; 
            padding: 15px; 
            background-color: #f8f9fa; 
            border-radius: 8px; 
            border-left: 4px solid #4CAF50; 
            color: #1f2937; 
            font-family: sans-serif;
        ">
            {safe_text}
        </div>
        """,
            unsafe_allow_html=True,
        )

    # 2. 查词交互区 (加上 st.form 以支持回车提交)
    with st.form(key="vocab_search_form"):
        col1, col2 = st.columns([3, 1])
        with col1:
            target_word = st.text_input(
                "📝 输入要查询的单词或短语 (如: factual record):", key="word_input"
            )
        with col2:
            st.markdown(
                "<div style='margin-top: 28px;'></div>", unsafe_allow_html=True
            )  # 精确占位对齐
            # 必须使用 st.form_submit_button 而不是 st.button
            search_btn = st.form_submit_button(
                "🔍 查询并制卡", use_container_width=True
            )

    if search_btn and target_word:
        with st.spinner(f"正在深度解析 '{target_word}' ..."):
            try:
                # 💡 专门为字典查询定制的 Prompt
                dict_prompt = f"""
                你是一本极其严谨的牛津/韦氏高级英语学习词典。
                现在用户正在阅读这篇文章：“{st.session_state['text']}”
                用户查询的词汇/短语是：“{target_word}”
                
                请你完成以下任务：
                1. 找到该词在文章中的原句，如果该词没有出现在原文，请找一个这个词常用的句子。
                2. 给出该词的**标准美式英语 IPA 音标**（如果是短语则可以省略）。注意：请务必确保音标100%准确，绝不能根据拼写规律凭空捏造！
                3. 结合文章上下文，给出该词最精准的中文释义，如果如果该词没有出现在原文，请给出这个词常用的意思。
                
                请严格按照以下格式输出（保留前面的标签名称）：
                音标: [填写标准美式英语IPA音标]
                释义: [填写中文释义]
                原句: [提取包含该词的完整英文原句，如果该词没有出现在原文中，用这个词造一个经典的句子]
                """

                response = model.generate_content(dict_prompt)
                res_text = response.text

                # 粗暴解析 AI 的输出
                phonetic = ""
                meaning = ""
                sentence = ""
                for line in res_text.split("\n"):
                    if line.startswith("音标:"):
                        phonetic = line.replace("音标:", "").strip()
                    if line.startswith("释义:"):
                        meaning = line.replace("释义:", "").strip()
                    if line.startswith("原句:"):
                        sentence = line.replace("原句:", "").strip()

                # 将查询结果展示出来
                st.success(f"解析成功！已自动加入待制卡队列。")
                st.markdown(f"**{target_word}** {phonetic}")
                st.markdown(f"**释义:** {meaning}")
                st.markdown(f"**上下文:** *{sentence}*")

                # 保存到待导出的记忆库中
                st.session_state["vocab_list"].append(
                    {
                        "word": target_word,
                        "phonetic": phonetic,
                        "meaning": meaning,
                        "sentence": sentence,
                    }
                )

            except Exception as e:
                st.error(f"查询失败: {e}")

    # 3. 词汇列表展示与 Anki 一键导出
    if st.session_state["vocab_list"]:
        st.markdown("### 🗂️ 你的专属词汇库 (等待导出)")

        # 💡 新增：带有删除按钮和丰富卡片内容的列表展示区
        for idx, item in enumerate(st.session_state["vocab_list"]):
            # 开启分列模式，左边占 11 份放文字，右边占 1 份放垃圾桶
            col_text, col_del = st.columns([11, 1])

            with col_text:
                # 💡 把单词、音标、释义、例句用 HTML 换行符 <br> 优雅地排版在一起
                st.markdown(
                    f"""
                **`{item['word']}`** {item['phonetic']} <br>
                💡 **释义:** {item['meaning']} <br>
                📖 **原句:** *{item['sentence']}*
                """,
                    unsafe_allow_html=True,
                )
                st.markdown("---")  # 加一条浅浅的分割线，让多张卡片层次分明

            with col_del:
                # 往下推一点，让垃圾桶在三行文字旁边视觉上垂直居中
                st.markdown(
                    "<div style='margin-top: 30px;'></div>", unsafe_allow_html=True
                )
                # 利用 idx 为每个按钮生成独一无二的 key，点击后瞬间删掉对应的卡片
                if st.button("🗑️", key=f"del_vocab_{idx}", help="删除这条生词"):
                    st.session_state["vocab_list"].pop(idx)
                    st.rerun()  # 强制刷新页面，让这行字瞬间消失

        # --- 核心：自动生成 Anki .apkg 文件链接 ---
        # 💡 终极修复：直接去掉 st.button，每次页面刷新都实时渲染下载链接，彻底稳住页面结构！

        # 定义 Anki 卡片模板 (正面：原句挖空/单词，背面：释义+音标+原句)
        my_model = genanki.Model(
            1607392319,
            "Native 特训舱词汇模型",
            fields=[
                {"name": "Word"},
                {"name": "Phonetic"},
                {"name": "Meaning"},
                {"name": "Context"},
            ],
            templates=[
                {
                    "name": "Card 1",
                    "qfmt": '<h2 style="text-align:center;">{{Word}}</h2><br><p style="text-align:center; color:gray;">{{Context}}</p>',
                    "afmt": '{{FrontSide}}<hr id="answer"><h3 style="text-align:center;">{{Phonetic}}</h3><h3 style="text-align:center; color:#e84118;">{{Meaning}}</h3>',
                },
            ],
        )

        # 创建牌组
        my_deck = genanki.Deck(
            2059400110, f"Native特训舱_{st.session_state['title'][:10]}"
        )

        # 将词汇塞进卡片并放入牌组
        for vocab in st.session_state["vocab_list"]:
            my_note = genanki.Note(
                model=my_model,
                fields=[
                    vocab["word"],
                    vocab["phonetic"],
                    vocab["meaning"],
                    vocab["sentence"],
                ],
            )
            my_deck.add_note(my_note)

        # 将牌组打包写入内存
        package = genanki.Package(my_deck)
        file_stream = io.BytesIO()
        package.write_to_file(file_stream)

        # 改用 Base64 原生 HTML 下载链接
        anki_bytes = file_stream.getvalue()
        b64 = base64.b64encode(anki_bytes).decode()
        safe_title = st.session_state["title"][:10].replace(" ", "_")

        download_html = f"""
        <a href="data:application/octet-stream;base64,{b64}" download="Native特训舱_{safe_title}.apkg" 
            style="display: inline-block; padding: 0.5em 1em; color: white; background-color: #FF4B4B; text-decoration: none; border-radius: 4px; font-family: sans-serif; font-weight: 500; margin-top: 10px;">
            📥 牌组已就绪！点击这里下载 Anki 卡片包 (.apkg)
        </a>
        """
        st.markdown(download_html, unsafe_allow_html=True)

    st.markdown("---")
    # == Step 4: 逐句原音重现与魔鬼通关 (Shadowing) ==
    st.subheader("Step 4: 逐句魔鬼跟读通关 (Shadowing)")
    st.info(
        "🗣️ 达到 90 分 (Native 水平) 即可自动解锁下一句。如果低于 90 分，仔细看教练反馈，你有 2 次重新挑战的机会！"
    )

    # 1. 初始化通关状态库
    if "s4_initialized" not in st.session_state:
        # 使用正则表达式，按句号、问号、叹号将全篇文章自动切分成单句
        raw_text = st.session_state["text"].replace("\n", " ")
        sentences = re.split(r"(?<=[.!?])\s+", raw_text)
        # 过滤掉太短的无效字符
        sentences = [s.strip() for s in sentences if len(s.strip()) > 5]

        st.session_state["s4_sentences"] = sentences
        st.session_state["s4_current_index"] = 0  # 当前读到第几句
        st.session_state["s4_retry_count"] = 0  # 当前句子重试了多少次
        st.session_state["s4_feedback"] = ""  # 存储教练的上一轮报错反馈
        st.session_state["s4_initialized"] = True

    sentences = st.session_state["s4_sentences"]
    curr_idx = st.session_state["s4_current_index"]

    # 2. 如果还没有通关全篇
    if curr_idx < len(sentences):
        target_sentence = sentences[curr_idx]

        # 进度条与重试状态指示
        progress = int((curr_idx / len(sentences)) * 100)
        st.progress(
            progress,
            text=f"全篇进度：{progress}% | 当前第 {curr_idx + 1}/{len(sentences)} 句",
        )

        st.markdown(f"### 🎯 目标句子：\n> **{target_sentence}**")

        # 如果有教练的上一轮反馈，强力展示出来
        # 如果有教练的上一轮反馈，使用折叠面板展示
        if st.session_state["s4_feedback"]:
            st.warning("⚠️ 发音未达标（需 90 分）。请参考下方反馈，调整状态后再试一次！")

            # 使用 st.expander 创建折叠区，expanded=False 表示默认是折叠收起的
            with st.expander(
                "📝 点击这里【展开 / 折叠】教练的详细纠音报告", expanded=False
            ):
                st.markdown(st.session_state["s4_feedback"])
        else:
            if st.session_state["s4_retry_count"] > 0:
                st.info(
                    f"🔄 这是你的第 {st.session_state['s4_retry_count']} 次重试，注意刚才纠正的发音细节！"
                )

        # 3. 自动为当前句子生成示范发音并缓存 (防弹版)
        audio_key = f"s4_audio_{curr_idx}"
        if audio_key not in st.session_state:
            with st.spinner("正在生成 Native 示范发音..."):
                try:
                    st.session_state[audio_key] = generate_fallback_audio(
                        target_sentence, f"demo_{curr_idx}.mp3"
                    )
                except Exception as e:
                    # 优雅捕获异常，绝不让程序崩溃
                    st.error(f"⚠️ 示范语音生成失败: {e}")
                    st.session_state[audio_key] = None

        st.markdown("**🎧 听完示范后点击录音：**")

        # 只有真正生成了音频，才显示播放器
        if st.session_state.get(audio_key):
            st.audio(st.session_state[audio_key])
        else:
            st.warning("⚠️ 暂无示范发音，但你依然可以直接看上面的句子录音打卡！")

        # ==========================================
        # 4. 用户录音组件
        # ==========================================
        current_art_id = st.session_state.get("current_id", "default_s4")

        shadow_audio_info = mic_recorder(
            start_prompt="▶️ 开始跟读",
            stop_prompt="⏹️ 结束跟读",
            key=f"shadow_rec_{current_art_id}_{curr_idx}_{st.session_state['s4_retry_count']}",
        )

        # 💡 核心修复 1：拿到稍纵即逝的录音后，立刻锁进长期记忆保险箱！
        lock_key = f"s4_audio_lock_{curr_idx}"
        if shadow_audio_info:
            st.session_state[lock_key] = shadow_audio_info["bytes"]

        # 5. 动态展示提交区域
        current_api_key = (
            locals().get("api_key")
            or globals().get("api_key")
            or st.secrets.get("GEMINI_API_KEY")
        )

        # 💡 核心修复 2：现在的判断条件，只看保险箱里有没有锁好的音频！
        if st.session_state.get(lock_key) and current_api_key:
            st.markdown("**✅ 你的录音：**")
            st.audio(st.session_state[lock_key], format="audio/wav")

            if st.button(
                "🚀 提交给教练打分",
                key=f"btn_score_{curr_idx}_{st.session_state['s4_retry_count']}",
            ):
                with st.spinner("教练正在拿着放大镜提取你的语音波形..."):
                    try:
                        with tempfile.NamedTemporaryFile(
                            delete=False, suffix=".wav"
                        ) as temp_audio:
                            # 💡 核心修复 3：从保险箱里读取音频发给 AI
                            temp_audio.write(st.session_state[lock_key])
                            temp_audio_path = temp_audio.name

                        audio_file = genai.upload_file(path=temp_audio_path)

                        # 💡 核心 Prompt：强制 AI 第一行只输出数字分数
                        shadow_prompt = f"""
                        你是一个以帮助学生提升英语口语发音为目的的美国 Native 英语口语教练。
                        学生的跟读目标文本是：“{target_sentence}”
                        请听附件中学生的录音。
                        
                        【极其重要的格式要求】：
                        第一行请务必**仅仅输出一个 0 到 100 的纯数字**（代表发音水平，90分以上为Native水平），绝对不要加任何标点符号或其他文字。
                        从第二行开始，按以下格式输出具体反馈（每个部分之间保留空行）：
                        【发音转录】: (精准写出你实际听到的学生发音)
                        【细节纠音】: (用中文指出具体的单词发音错误、重音错误、连读缺失)
                        【Native 诀窍】: (用中文说明如何像本地人一样处理这些细节)
                        """

                        response = model.generate_content([shadow_prompt, audio_file])
                        reply_text = response.text.strip()

                        # 强悍的正则解析：把 AI 吐出来的第一行的分数挖出来
                        lines = reply_text.split("\n")
                        try:
                            score_match = re.search(r"\d+", lines[0])
                            score = int(score_match.group()) if score_match else 0
                        except:
                            score = 0

                        # 解析后面的教练评语并强制排版
                        feedback_body = (
                            "\n".join(lines[1:]).replace("**", "").replace("#", "")
                        )
                        feedback_body = feedback_body.replace(
                            "【发音转录】", "\n\n**【发音转录】**\n"
                        )
                        feedback_body = feedback_body.replace(
                            "【细节纠音】", "\n\n**【细节纠音】**\n"
                        )
                        feedback_body = feedback_body.replace(
                            "【Native 诀窍】", "\n\n**【Native 诀窍】**\n"
                        ).strip()

                        os.remove(temp_audio_path)

                        if lock_key in st.session_state:
                            del st.session_state[lock_key]

                        if score >= 90:
                            st.session_state["s4_feedback"] = ""
                            st.session_state["s4_current_index"] += 1
                            st.session_state["s4_retry_count"] = 0
                            st.success(
                                f"🎉 你的得分是 **{score}**！极其完美的 Native 发音，直接解锁下一句！"
                            )
                            time.sleep(3)
                            st.rerun()
                        else:
                            st.session_state["s4_retry_count"] += 1
                            if st.session_state["s4_retry_count"] >= 2:
                                st.session_state["s4_feedback"] = ""
                                st.session_state["s4_current_index"] += 1
                                st.session_state["s4_retry_count"] = 0
                                st.error(
                                    f"⚠️ 你的得分是 **{score}**。你已用尽 2 次重试机会。不要气馁，为了保持训练节奏，我们先进入下一句！"
                                )
                                time.sleep(4)
                                st.rerun()
                            else:
                                st.session_state["s4_feedback"] = (
                                    f"❌ 你的得分是 **{score}** (通关需 90分)。\n\n**教练反馈：**\n{feedback_body}\n\n👉 请仔细看上面的纠音，再挑战一次！"
                                )
                                st.rerun()

                    except Exception as e:
                        st.error(f"打分失败: {e}")
    else:
        if not st.session_state.get("s5_has_fired_balloons", False):
            st.balloons()
            st.session_state["s5_has_fired_balloons"] = True
        st.success(
            "🏆 太了不起了！你完成了全篇所有句子的逐句跟读特训！你的发音肌肉记忆已经形成了初步的 Native 语感。"
        )
    st.markdown("---")

    # ==========================================
    # == Step 5: 全篇影子跟读 (Shadowing) ==
    # ==========================================
    st.subheader("Step 5: 全篇影子跟读 (Shadowing)")
    st.info(
        "🎧 **核心口语训练：** 请播放原声音频，看着下方的文本，**尽最大努力模仿 Native Speaker 的发音、语调、连读和节奏**。目标是完成 3 遍高频跟读！"
    )

    # 1. 播放原声音频
    # (假设你的音频链接存放在 st.session_state['audio_url'] 中，如果变量名不同请自行替换)
    if st.session_state.get("audio_url"):
        st.audio(st.session_state["audio_url"])
    else:
        st.warning("⚠️ 暂无音频资源，你可以自己大声朗读一遍。")

    # 2. 极其稳定的防弹版 HTML 文本展示（带有内部滚动条，不撑爆网页）
    with st.container():
        safe_text = st.session_state.get("text", "").replace("\n", "<br>")
        st.markdown(
            f"""
        <div style="
            font-size: 16px; 
            line-height: 1.8; 
            padding: 15px; 
            background-color: #f8f9fa; 
            border-radius: 8px; 
            border-left: 4px solid #4CAF50; 
            color: #1f2937; 
            font-family: sans-serif;
            height: 350px; 
            overflow-y: auto; 
            margin-bottom: 20px;
        ">
            {safe_text}
        </div>
        """,
            unsafe_allow_html=True,
        )

    # 3. 三遍强制打卡系统（仪式感拉满）
    st.markdown("### 🎯 影子跟读打卡区")

    # 开启三分列布局，排版整齐
    col1, col2, col3 = st.columns(3)
    with col1:
        round1 = st.checkbox("✅ 第 1 遍 (熟悉文本与发音)", key="s5_round1")
    with col2:
        round2 = st.checkbox("✅ 第 2 遍 (攻克连读与弱读)", key="s5_round2")
    with col3:
        round3 = st.checkbox("✅ 第 3 遍 (追求语调与节奏)", key="s5_round3")

    # 当三遍都勾选时，给予正向反馈和庆祝
    if round1 and round2 and round3:
        st.success(
            "🎉 太自律了！你已经完成了 3 遍高强度影子跟读，口腔肌肉记忆正在形成！请继续进入 Step 6。"
        )

        # 💡 你最熟悉的防手抖气球锁！
        if not st.session_state.get("s5_balloons_fired", False):
            st.balloons()
            st.session_state["s5_balloons_fired"] = True

    st.markdown("---")

    # ==========================================
    # == Step 6: 终极脱稿复述大决战 ==
    # ==========================================
    st.subheader("Step 6: 终极脱稿复述大决战 (Retelling)")
    st.info(
        "🔥 终极挑战！请在不看原文的情况下，用你自己的话把整篇文章复述一遍。建议先提取关键词作为提示，再开始录音。"
    )

    # 💡 核心防护1：极其稳定的 API Key 获取方式
    current_api_key = st.secrets.get("GEMINI_API_KEY") or st.session_state.get(
        "saved_api_key"
    )

    # --- 1. 关键词提取区 (脚手架) ---
    if "s6_keywords" not in st.session_state:
        st.session_state["s6_keywords"] = ""

    if st.button("💡 脑子一片空白？点击提取核心关键词线索"):
        with st.spinner("考官正在为你提取故事主线..."):
            try:
                kw_prompt = f"""
                请从以下文章中提取 5 到 10 个核心英文关键词（按逻辑顺序）方便按照原文逻辑提示复述，用 -> 连接。
                文章原文：{st.session_state['text']}
                """
                if current_api_key:
                    genai.configure(api_key=current_api_key)
                    temp_model = genai.GenerativeModel("gemini-3.1-flash-lite-preview")
                    kw_response = temp_model.generate_content(kw_prompt)
                    st.session_state["s6_keywords"] = (
                        kw_response.text.replace("**", "").replace("#", "").strip()
                    )
                    st.rerun()
                else:
                    st.error("⚠️ 未检测到 API Key，请先在侧边栏配置。")
            except Exception as e:
                st.error(f"提取失败: {e}")

    if st.session_state["s6_keywords"]:
        st.success(f"🔑 **复述主线支架：** {st.session_state['s6_keywords']}")

    st.write("")

    # --- 2. 录音与提交区 (核心交互) ---

    # 💡 新增机制：给 Step 6 也加上一个独立的“重生计数器”
    if "s6_retry_count" not in st.session_state:
        st.session_state["s6_retry_count"] = 0

    current_art_id = st.session_state.get("current_id", "default_retell")

    # 💡 核心修复：把重生计数器加进 Key 里！
    retell_audio_info = mic_recorder(
        start_prompt="▶️ 开始全篇复述",
        stop_prompt="⏹️ 结束复述并试听",
        key=f"s6_retell_rec_{current_art_id}_{st.session_state['s6_retry_count']}",
    )

    # 💡 核心防护：一旦拿到录音，立刻锁进长期记忆
    if retell_audio_info:
        st.session_state["s6_audio_bytes_locked"] = retell_audio_info["bytes"]

    # 基于长期记忆来判断是否显示提交按钮
    if st.session_state.get("s6_audio_bytes_locked") and current_api_key:
        st.markdown("**✅ 你的复述录音：**")
        st.audio(st.session_state["s6_audio_bytes_locked"], format="audio/wav")

        # 💡 连同提交按钮的名字也加上计数器，防止状态残留
        if st.button(
            "🚀 提交终极复述进行批改",
            key=f"btn_retell_{st.session_state['s6_retry_count']}",
        ):
            with st.spinner("AI 考官正在全面评估中..."):
                try:
                    with tempfile.NamedTemporaryFile(
                        delete=False, suffix=".wav"
                    ) as temp_audio:
                        temp_audio.write(st.session_state["s6_audio_bytes_locked"])
                        temp_audio_path = temp_audio.name

                    genai.configure(api_key=current_api_key)
                    temp_model = genai.GenerativeModel("gemini-3.1-flash-lite-preview")
                    audio_file = genai.upload_file(path=temp_audio_path)

                    retell_prompt = f"""
                    你是一个严苛的美国 Native 英语口语教练。
                    文章原文：“{st.session_state['text']}”
                    请听录音并按格式反馈：
                    【你的原话】: ...
                    【复述完整度】: ...
                    【纠错与评价】: ...
                    【Native 优化】: ... (不带#号)
                    """
                    response = temp_model.generate_content([retell_prompt, audio_file])

                    st.session_state["s6_feedback"] = (
                        response.text.replace("**", "")
                        .replace("#", "")
                        .replace("：", ":")
                    )

                    os.remove(temp_audio_path)

                    # 💡 核心修复终结步：批改完成后，清空音频记忆，并让“重生计数器”加 1！
                    del st.session_state["s6_audio_bytes_locked"]
                    st.session_state["s6_retry_count"] += 1

                    st.rerun()

                except Exception as e:
                    st.error(f"提交失败: {e}")

    # --- 3. 结果展示区 ---
    if "s6_feedback" in st.session_state:
        st.markdown("---")
        st.success("🎉 终极挑战批改完成！")
        st.markdown(st.session_state["s6_feedback"])

        if not st.session_state.get("s6_has_fired_balloons", False):
            st.balloons()
            st.session_state["s6_has_fired_balloons"] = True

    st.markdown("---")
    # ==========================================
    # == Step 7: 纯英文闲聊 (Free Talk) ==
    # ==========================================
    st.subheader("Step 7: 纯英文闲聊 (Free Talk)")
    st.info(
        "☕ 忘掉原文、忘掉语法、忘掉翻译！现在我们像朋友一样，用纯英文随便聊聊这篇文章里涉及的话题。对话将限制在 6 轮以内，尽情表达吧！"
    )

    # 💡 核心防护 1：极其稳定的 API Key 雷达
    current_api_key = st.secrets.get("GEMINI_API_KEY") or st.session_state.get(
        "saved_api_key"
    )

    # 1. 初始化聊天记忆库与回合计数器
    if "s7_chat_history" not in st.session_state:
        st.session_state["s7_chat_history"] = []
        st.session_state["s7_turn_count"] = 0

        # 定义静态开场白文本
        opener_text = "Hey there! I just finished reading that article too. It really got me thinking... What was your biggest takeaway from it?"

        # 将文本存入记忆
        st.session_state["s7_chat_history"].append(
            {"role": "assistant", "content": opener_text}
        )

        # 💡 核心防护 2：给开场白语音加上防瘫痪保护
        with st.spinner("Preparing the opening..."):
            try:
                tts_opener_file = generate_fallback_audio(
                    opener_text, "chat_opener.mp3"
                )
                st.session_state["s7_latest_audio"] = tts_opener_file
            except Exception as e:
                st.error(f"⚠️ 开场语音生成失败: {e}")
                st.session_state["s7_latest_audio"] = None

    # 2. 渲染聊天气泡
    for msg in st.session_state["s7_chat_history"]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # 3. 自动播放 AI 的最新语音回复
    if (
        "s7_latest_audio" in st.session_state
        and st.session_state["s7_latest_audio"]
        and os.path.exists(st.session_state["s7_latest_audio"])
    ):
        st.audio(st.session_state["s7_latest_audio"])

    # 4. 闲聊录音组件与轮数控制
    if st.session_state["s7_turn_count"] < 6:
        st.markdown(
            f"**🗣️ 当前对话进度：{st.session_state['s7_turn_count'] + 1} / 6 轮**"
        )

        chat_audio_info = mic_recorder(
            start_prompt="▶️ 开始录音",
            stop_prompt="⏹️ 结束录音",
            key=f"chat_rec_{st.session_state['s7_turn_count']}",
        )

        # 💡 核心防护 3：拿到录音立刻锁进长期记忆
        if chat_audio_info:
            st.session_state["s7_audio_bytes_locked"] = chat_audio_info["bytes"]

        # 基于长期记忆和 API Key 存在与否来判断是否显示发送按钮
        if st.session_state.get("s7_audio_bytes_locked") and current_api_key:
            # 让你在发送前能自己听一下录得对不对
            st.audio(st.session_state["s7_audio_bytes_locked"], format="audio/wav")

            if st.button(
                "🚀 确认无误，发送给朋友",
                key=f"btn_chat_{st.session_state['s7_turn_count']}",
            ):
                with st.spinner("你的朋友正在思考并回复..."):
                    # 重新挂载大模型，确保万无一失
                    genai.configure(api_key=current_api_key)
                    temp_model = genai.GenerativeModel("gemini-3.1-flash-lite-preview")

                    # 第一步：保存录音和准备 Prompt (本地操作，不会断网)
                    with tempfile.NamedTemporaryFile(
                        delete=False, suffix=".wav"
                    ) as temp_audio:
                        temp_audio.write(st.session_state["s7_audio_bytes_locked"])
                        temp_audio_path = temp_audio.name

                    # 提取最近的对话上下文，让 AI 有“短期记忆”
                    chat_context = ""
                    for msg in st.session_state["s7_chat_history"][-4:]:
                        chat_context += f"{msg['role']}: {msg['content']}\n"

                    # 修复 Bug：正确定义指令
                    if st.session_state["s7_turn_count"] == 5:
                        ending_instruction = "5. 这是我们对话的最后一轮，请自然地总结我们的聊天，并跟我道别（Say goodbye）。绝对不要再提出任何新问题了！"
                    else:
                        ending_instruction = "5. 在最后自然地抛出一个相关的问题（比如 'What about you?' 或 'Have you ever experienced something similar?'），引导我继续说下去。"

                    chat_prompt = f"""
                    你现在是我的一个美国朋友（Native English Speaker）。我们正在喝咖啡闲聊，话题是基于这篇文章拓展的：“{st.session_state['text']}”。
                    
                    这是我们刚才的对话上下文：
                    {chat_context}
                    
                    现在请听附件里我刚发给你的语音回复。
                    
                    【严格任务要求】：
                    1. 直接用极其自然、地道的日常美国口语回复我（多用连词、习语，语气要像真正的朋友聊天）。
                    2. 绝对不要出现任何中文！请像朋友一样纠正我口语中的错误，但不要出现打分或评语！
                    3. 像朋友一样表达你的赞同、惊讶或提出不同视角。
                    4. 回复控制在 3-4 句话以内。
                    {ending_instruction}
                    """

                    # 第二步：🎯 核心网络重试机制 (最多尝试 3 次)
                    max_retries = 3
                    reply_text = None

                    for attempt in range(max_retries):
                        try:
                            # 这些容易断开的操作，都用 temp_model
                            audio_file = genai.upload_file(path=temp_audio_path)
                            response = temp_model.generate_content(
                                [chat_prompt, audio_file]
                            )

                            # 拿到结果，清洗格式
                            reply_text = (
                                response.text.replace("**", "").replace("#", "").strip()
                            )
                            break  # 💡 成功了！立刻打破循环跳出

                        except Exception as e:
                            error_msg = str(e)
                            if "429" in error_msg or "Quota" in error_msg:
                                st.warning(
                                    "⏳ 你的朋友正在喝水，触发频率限制，请稍等片刻后再发消息。"
                                )
                                break
                            elif attempt < max_retries - 1:
                                time.sleep(2)
                                continue
                            else:
                                st.error(
                                    f"⚠️ 网络代理极不稳定，已重试 3 次仍被强制中断连接: {e}"
                                )
                                break

                    # 第三步：如果成功拿到了回复，执行记忆保存和网页刷新
                    if reply_text:
                        # 将记录保存到记忆库
                        st.session_state["s7_chat_history"].append(
                            {"role": "user", "content": "🎤 *(Voice message sent)*"}
                        )
                        st.session_state["s7_chat_history"].append(
                            {"role": "assistant", "content": reply_text}
                        )

                        # 生成 AI 的语音回复
                        try:
                            tts_file = generate_fallback_audio(
                                reply_text,
                                f"chat_reply_{st.session_state['s7_turn_count']}.mp3",
                            )
                            st.session_state["s7_latest_audio"] = tts_file
                        except Exception as e:
                            st.error(f"⚠️ 回复语音生成失败: {e}")
                            st.session_state["s7_latest_audio"] = None

                        # 回合数 +1，准备进入下一轮
                        st.session_state["s7_turn_count"] += 1

                        # 垃圾清理
                        os.remove(temp_audio_path)
                        # 💡 极其重要：解锁音频记忆，为下一轮录音腾出空间！
                        del st.session_state["s7_audio_bytes_locked"]

                        st.rerun()

        elif st.session_state.get("s7_audio_bytes_locked") and not current_api_key:
            st.warning("⚠️ 录音已准备好，但请先配置 API Key 才能发送哦！")

    else:
        # 当 6 轮满员后，隐藏录音区，展示终极撒花庆祝
        st.success(
            """
            🎉 **今日特训圆满达成！** 6 轮深度闲聊已完成，你不仅成功复述了文章，还进行了高质量的即兴表达。  
            今天的高强度 Native 特训到此结束，快去给大脑充个电吧！
            """
        )

        # 仪式感气球（放在锁里面，确保只放一次，绝不复读）
        if not st.session_state.get("s7_final_balloons_fired", False):
            st.balloons()
            st.toast("🏆 任务达成！Level Up!")
            st.session_state["s7_final_balloons_fired"] = True
