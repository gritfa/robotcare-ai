import { describe, expect, it, vi } from 'vitest'
import { uploadAttachmentQueue } from './attachmentUploadQueue'

describe('uploadAttachmentQueue', () => {
  it('stops on the first failure and keeps the failed and remaining files for retry', async () => {
    const files = [
      new File(['one'], 'one.png', { type: 'image/png' }),
      new File(['two'], 'two.png', { type: 'image/png' }),
      new File(['three'], 'three.png', { type: 'image/png' }),
    ]
    const limited = new Error('rate limited')
    const upload = vi.fn()
      .mockResolvedValueOnce({ id: 1 })
      .mockRejectedValueOnce(limited)

    const result = await uploadAttachmentQueue(files, upload)

    expect(upload).toHaveBeenCalledTimes(2)
    expect(result.uploaded).toEqual([{ id: 1 }])
    expect(result.pending.map(file => file.name)).toEqual(['two.png', 'three.png'])
    expect(result.error).toBe(limited)
  })
})
