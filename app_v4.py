# ==========================================
    # == Step 6: 终极脱稿复述大决战 ==
    # ==========================================
    st.subheader("Step 6: 终极脱稿复述大决战 (Retelling)")
    st.info(
        "🔥 终极挑战！请在不看原文的情况下，用你自己的话把整篇文章复述一遍。建议先提取关键词作为提示，再开始录音。"
    )

    # 💡 核心防护：安全获取全局 API Key，防止变量丢失
    current_api_key = locals().get("api_key") or globals().get("api_key") or st.secrets.get("GEMINI_API_KEY")

    # --- 1. 关键词提取区 (脚手架) ---
    if "s6_keywords" not in st.session_state:
        st.session_state["s6_keywords"] = ""

    # 提取按钮始终可见
    if st.button("💡 脑子一片空白？点击提取核心关键词线索"):
        with st.spinner("考官正在为你提取故事主线..."):
            try:
                kw_prompt = f"""
                请从以下文章中提取 5 到 10 个核心英文关键词（按逻辑顺序）方便按照原文逻辑提示复述，用 -> 连接。
                文章原文：{st.session_state['text']}
                """
                
                # 重新挂载大模型，确保万无一失
                if current_api_key:
                    genai.configure(api_key=current_api_key)
                    temp_model = genai.GenerativeModel("gemini-1.5-flash")
                    kw_response = temp_model.generate_content(kw_prompt)
                    
                    st.session_state["s6_keywords"] = (
                        kw_response.text.replace("**", "").replace("#", "").strip()
                    )
                    st.rerun()  # 刷新以展示关键词
                else:
                    st.error("⚠️ 未检测到 API Key，请先在侧边栏配置。")
            except Exception as e:
                st.error(f"提取失败: {e}")

    # 如果有关键词，优美地展示
    if st.session_state["s6_keywords"]:
        st.success(f"🔑 **复述主线支架：** {st.session_state['s6_keywords']}")

    st.write("")  # 留点间距

    # --- 2. 录音与提交区 (核心交互) ---
    # 录音组件必须在主循环中，确保始终显示
    retell_audio_info = mic_recorder(
        start_prompt="▶️ 开始全篇复述",
        stop_prompt="⏹️ 结束复述并试听",
        key="retell_recorder",
    )

    # 💡 双重验证：既要有录音，也要有 API Key
    if retell_audio_info and current_api_key:
        retell_audio_bytes = retell_audio_info["bytes"]
        st.markdown("**✅ 你的复述录音：**")
        st.audio(retell_audio_bytes, format="audio/wav")

        # 只有在有录音的情况下，才显示“提交”按钮
        if st.button("🚀 提交终极复述进行批改", key="btn_retell"):
            with st.spinner("AI 考官正在全面评估中..."):
                try:
                    with tempfile.NamedTemporaryFile(
                        delete=False, suffix=".wav"
                    ) as temp_audio:
                        temp_audio.write(retell_audio_bytes)
                        temp_audio_path = temp_audio.name

                    # 再次挂载，确保长流程中连接不断开
                    genai.configure(api_key=current_api_key)
                    temp_model = genai.GenerativeModel("gemini-1.5-flash")
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

                    # 清洗文本并存入记忆
                    clean_feedback = (
                        response.text.replace("**", "")
                        .replace("#", "")
                        .replace("：", ":")
                    )
                    st.session_state["s6_feedback"] = clean_feedback

                    os.remove(temp_audio_path)  # 清理临时文件
                    st.rerun()  # 提交成功后刷新页面以持久化显示结果

                except Exception as e:
                    st.error(f"提交失败: {e}")
                    
    elif retell_audio_info and not current_api_key:
         st.warning("⚠️ 录音已完成，但请先在左侧边栏配置 API Key 才能提交批改！")

    # --- 3. 结果展示区 (防消失逻辑) ---
    # 注意：这段代码在 if retell_audio_info 之外，确保结果一旦生成就永远显示
    if "s6_feedback" in st.session_state:
        st.markdown("---")
        st.success("🎉 终极挑战批改完成！")
        st.markdown(st.session_state["s6_feedback"])

        if not st.session_state.get("s6_has_fired_balloons", False):
            st.balloons()
            st.session_state["s6_has_fired_balloons"] = True

    st.markdown("---")
