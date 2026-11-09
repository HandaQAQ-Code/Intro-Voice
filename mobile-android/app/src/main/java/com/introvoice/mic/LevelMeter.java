package com.introvoice.mic;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.LinearGradient;
import android.graphics.Paint;
import android.graphics.RectF;
import android.graphics.Shader;
import android.util.AttributeSet;
import android.view.View;

/** 输入电平条：底槽 + 渐变填充 + 峰值保持刻度，按 dBFS 映射。 */
public final class LevelMeter extends View {
    private static final float MIN_DB = -60f;

    private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final RectF rect = new RectF();
    private final float density;
    private float level = MIN_DB;
    private float peak = MIN_DB;
    private long peakAt = 0L;

    public LevelMeter(Context context) {
        this(context, null);
    }

    public LevelMeter(Context context, AttributeSet attrs) {
        super(context, attrs);
        density = getResources().getDisplayMetrics().density;
    }

    public void setLevelDb(float db) {
        level = Math.max(MIN_DB, Math.min(0f, db));
        if (level >= peak) {
            peak = level;
            peakAt = System.currentTimeMillis();
        } else if (System.currentTimeMillis() - peakAt > 700) {
            peak = Math.max(level, peak - 0.8f);
        }
        invalidate();
    }

    public void reset() {
        level = MIN_DB;
        peak = MIN_DB;
        invalidate();
    }

    @Override
    protected void onDraw(Canvas canvas) {
        super.onDraw(canvas);
        float w = getWidth();
        float h = getHeight();
        float radius = h / 2f;

        paint.setShader(null);
        paint.setColor(0xFF0E0B09);
        rect.set(0, 0, w, h);
        canvas.drawRoundRect(rect, radius, radius, paint);

        float ratio = (level - MIN_DB) / -MIN_DB;
        float fillW = Math.max(0f, Math.min(1f, ratio)) * w;
        if (fillW > radius * 2f) {
            paint.setShader(new LinearGradient(0, 0, w, 0, new int[]{
                    0xFF6FBF7A, 0xFFE8B24A, 0xFFE8744A}, null, Shader.TileMode.CLAMP));
            rect.set(0, 0, fillW, h);
            canvas.drawRoundRect(rect, radius, radius, paint);
            paint.setShader(null);
        }

        float peakRatio = (peak - MIN_DB) / -MIN_DB;
        float px = Math.max(radius, Math.min(1f, peakRatio) * w - density);
        if (peak > MIN_DB + 1f) {
            paint.setColor(0xFFF7F1E6);
            rect.set(px - density, density, px + density, h - density);
            canvas.drawRoundRect(rect, density, density, paint);
        }
    }
}
