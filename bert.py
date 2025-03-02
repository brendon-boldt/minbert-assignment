from typing import Dict, List, Optional, Union, Tuple, Callable
import math
import torch
import torch.nn as nn
import torch.nn.functional as F
from base_bert import BertPreTrainedModel
from utils import *

"""
Notes
1. seems a little bit unstable (see TODO
"""

class BertSelfAttention(nn.Module):
  def __init__(self, config):
    super().__init__()

    self.num_attention_heads = config.num_attention_heads
    self.attention_head_size = int(config.hidden_size / config.num_attention_heads)
    self.all_head_size = self.num_attention_heads * self.attention_head_size

    # initialize the linear transformation layers for key, value, query
    self.query = nn.Linear(config.hidden_size, self.all_head_size)
    self.key = nn.Linear(config.hidden_size, self.all_head_size)
    self.value = nn.Linear(config.hidden_size, self.all_head_size)
    self.dropout = nn.Dropout(config.attention_probs_dropout_prob)

  def transform(self, x, linear_layer):
    # student
    # split the hidden states to num_attention_heads for multi-head attention
    # hidden_size = self.num_attention_heads * self.attention_head_size
    # x [batch_size, seq_len, hidden_size]
    bs, seq_len = x.shape[:2]
    proj = linear_layer(x)
    proj = proj.view(bs, seq_len, self.num_attention_heads, self.attention_head_size)
    proj = proj.transpose(1, 2)
    return proj

  def attention(self, key, query, value, attention_mask):
    # student
    # eq (1) of https://arxiv.org/pdf/1706.03762.pdf
    attention_scores = torch.matmul(query, key.transpose(-1, -2))
    attention_scores = attention_scores / math.sqrt(self.attention_head_size)
    attention_scores = attention_scores + attention_mask
    # normalize to probability
    attention_probs = nn.Softmax(dim=-1)(attention_scores)
    # attention_probs = self.dropout(attention_probs)
    attn_value = torch.matmul(attention_probs, value)
    # recover the original shape
    attn_value = attn_value.transpose(1, 2).contiguous()
    bs, seq_len = attn_value.shape[:2]
    attn_value = attn_value.view(bs, seq_len, -1)
    return attn_value

  def forward(self, hidden_states, attention_mask):
    key_layer = self.transform(hidden_states, self.key)
    value_layer = self.transform(hidden_states, self.value)
    query_layer = self.transform(hidden_states, self.query)

    attn_value = self.attention(key_layer, query_layer, value_layer, attention_mask)
    return attn_value


class BertLayer(nn.Module):
  def __init__(self, config):
    super().__init__()
    # self attention
    self.self_attention = BertSelfAttention(config)
    self.attention_dense = nn.Linear(config.hidden_size, config.hidden_size)
    self.attention_layer_norm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)
    self.attention_dropout = nn.Dropout(config.hidden_dropout_prob)
    # feed forward
    self.interm_dense = nn.Linear(config.hidden_size, config.intermediate_size)
    self.interm_af = F.gelu
    # layer ouit
    self.out_dense = nn.Linear(config.intermediate_size, config.hidden_size)
    self.out_layer_norm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)
    self.out_dropout = nn.Dropout(config.hidden_dropout_prob)

  def add_norm(self, input, output, dense_layer, dropout, ln_layer):
    # student
    proj = dense_layer(output)
    proj = dropout(proj)
    residual = proj + input
    ln = ln_layer(residual)
    return ln

  def forward(self, hidden_states, attention_mask):
    # student
    attn_outputs = self.self_attention(hidden_states, attention_mask)

    # add-norm layer
    attn_outputs = self.add_norm(hidden_states, attn_outputs, self.attention_dense, self.attention_dropout, self.attention_layer_norm)

    # feed forward
    proj = self.interm_dense(attn_outputs)
    proj = self.interm_af(proj)

    # another add-norm layer
    layer_output = self.add_norm(attn_outputs, proj, self.out_dense, self.out_dropout, self.out_layer_norm)

    return layer_output


class BertModel(BertPreTrainedModel):
  def __init__(self, config):
    super().__init__(config)
    self.config = config

    # embedding
    self.word_embedding = nn.Embedding(config.vocab_size, config.hidden_size, padding_idx=config.pad_token_id)
    self.pos_embedding = nn.Embedding(config.max_position_embeddings, config.hidden_size)
    self.tk_type_embedding = nn.Embedding(config.type_vocab_size, config.hidden_size)
    self.embed_layer_norm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)
    self.embed_dropout = nn.Dropout(config.hidden_dropout_prob)
    # position_ids (1, len position emb) is a constant, register to buffer
    position_ids = torch.arange(config.max_position_embeddings).unsqueeze(0)
    self.register_buffer('position_ids', position_ids)

    # bert encoder
    self.bert_layers = nn.ModuleList([BertLayer(config) for _ in range(config.num_hidden_layers)])

    # for [CLS] token
    self.pooler_dense = nn.Linear(config.hidden_size, config.hidden_size)
    self.pooler_af = nn.Tanh()

    self.init_weights()

  def embed(self, input_ids):
    input_shape = input_ids.size()
    seq_length = input_shape[1]

    # get word embedding
    inputs_embeds = self.word_embedding(input_ids)

    # get position index and position embedding
    pos_ids = self.position_ids[:, :seq_length]
    pos_embeds = self.pos_embedding(pos_ids)

    # get token type ids, since we are not consider token type, just a placeholder
    tk_type_ids = torch.zeros(input_shape, dtype=torch.long, device=input_ids.device)
    tk_type_embeds = self.tk_type_embedding(tk_type_ids)

    # add three embeddings together, TODO, check why change order change the results
    embeds = inputs_embeds + tk_type_embeds + pos_embeds

    # layer norm and dropout
    embeds = self.embed_layer_norm(embeds)
    embeds = self.embed_dropout(embeds)
    return embeds
    # return embeddings

  def encode(self, hidden_states, attention_mask):
    # get the extended attention mask for self attention
    extended_attention_mask: torch.Tensor = get_extended_attention_mask(attention_mask, self.dtype)

    # pass the hidden states through the encoder layers
    for i, layer_module in enumerate(self.bert_layers):
      hidden_states = layer_module(hidden_states, extended_attention_mask)

    return hidden_states

  def forward(self, input_ids, attention_mask):
    # get the embedding for each input token
    embedding_output = self.embed(input_ids=input_ids)
    # feed to a transformer (a stack of BertLayers)
    sequence_output = self.encode(embedding_output, attention_mask=attention_mask)

    # get cls token hidden state
    first_tk = sequence_output[:, 0]
    first_tk = self.pooler_dense(first_tk)
    first_tk = self.pooler_af(first_tk)

    return {'last_hidden_state': sequence_output, 'pooler_output': first_tk}
