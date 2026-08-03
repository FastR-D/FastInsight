# Research Direction Taxonomy (starter)

A starting set of research directions with keyword hints. Copy the ones relevant
to your team into `colleagues.yaml` and adjust keywords to match how papers in
your field are actually titled. Matching is case-insensitive over
`title + abstract + venue`: ASCII keywords use token boundaries, while Chinese
and mixed-language keywords use phrase matching.

> Tip: keep keywords specific. Token boundaries prevent `RL` from matching
> "URL", but short acronyms can still be ambiguous in abstracts.

## Natural Language Processing / LLM
- keywords: LLM, large language model, GPT, instruction tuning, RLHF, prompt,
  chain-of-thought, pre-train, chatbot, 大语言模型, 预训练, 指令微调, 对齐
- owners: (同学 A)

## Computer Vision
- keywords: vision, image, object detection, segmentation, diffusion, NeRF,
  video, 视觉, 目标检测, 图像分割, 扩散模型
- owners: (同学 B)

## Multimodal
- keywords: multimodal, vision-language, VLM, image-text, video-text, 多模态
- owners: (同学 C)

## Reinforcement Learning
- keywords: reinforcement learning, policy gradient, MDP, actor-critic,
  reward model, 强化学习, 策略梯度
- owners: (同学 D)

## Graph / GNN
- keywords: graph neural, GNN, node classification, knowledge graph, 图神经网络,
  知识图谱
- owners: (同学 E)

## Speech / Audio
- keywords: speech, ASR, TTS, audio, speaker, 语音, 语音识别, 语音合成
- owners: (同学 F)

## Recommendation / Search
- keywords: recommendation, retrieval, ranking, CTR, 推荐系统, 搜索, 排序
- owners: (同学 G)

## ML Systems / Inference
- keywords: inference, quantization, distillation, serving, compiler, 推理加速,
  模型压缩, 量化
- owners: (同学 H)

## Robotics / Embodied AI
- keywords: robot, manipulation, navigation, embodied, 机器人, 具身智能
- owners: (同学 I)

## Security / Privacy
- keywords: adversarial, privacy, differential privacy, membership inference,
  对抗攻击, 隐私
- owners: (同学 J)

## Bioinformatics / Healthcare AI
- keywords: biomedical, protein, genomics, healthcare, EHR, 生物信息, 医疗
- owners: (同学 K)

## Theory / Optimization
- keywords: convergence, regret, generalization bound, optimization, 收敛性,
  泛化, 优化理论
- owners: (同学 L)

## News / Industry (non-paper)
- keywords: report, funding, launch, open-source release, 融资, 发布, 开源
- owners: (组长 / 全员) — often the `default_owner`

### How to extend
- Add a `directions` entry per active sub-field in your team.
- Put the most distinctive terms first; they dominate scoring.
- If a paper could belong to two directions, the higher keyword score wins. Add
  `priority` to break ties explicitly; otherwise the earlier entry wins.
