def log_controls():
    return """
<div class="fls-log-float">
    <button type="button" onclick="flsLogCopyAll()" title="复制全部日志">⧉</button>
    <button type="button" onclick="flsLogGoTop()" title="回到顶部">↑</button>
    <button type="button" onclick="flsLogGoBottom()" title="到底部">↓</button>
</div>
<button type="button" class="fls-log-new-tip" id="flsLogNewTip" onclick="flsLogGoBottom()">有新日志，点击到底部</button>
"""
